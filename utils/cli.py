import json
import math
import pathlib
import shutil
import sys
import time
from collections import namedtuple
from itertools import chain

from .errors import BdecodeError, EmptySourceSize, PieceSizeUncommon, PieceSizeTooSmall
from .torrent import Torrent
import os

class Path(type(pathlib.Path())):

    def is_file_(self):
        """Is file (not torrent)."""
        return self.is_file() and self.suffix.lower() != '.torrent'

    def is_virtual_file_(self):
        """Is virtual file (not torrent)."""
        return not self.is_dir() and self.suffix.lower() != '.torrent'

    def is_torrent_(self):
        """Is torrent."""
        return self.is_file() and self.suffix.lower() == '.torrent'

    def is_virtual_torrent_(self):
        """Is virtual torrent."""
        return not self.is_dir() and self.suffix.lower() == '.torrent'

    def is_dir_(self):
        """Is directory."""
        return self.is_dir()

    def is_virtual_dir_(self):
        """Is virtual directory."""
        return self.is_dir() or not self.is_file()


class Main:

    def __init__(self, args):
        self.torrent = Torrent()

        # extract cli config from cli arguments
        self.cfg = self.__pick_cli_cfg(args)
        # infer `mode` from the properties of supplied paths if not specified by the user
        self.mode = self.__pick_mode(args.mode, args.fpaths)
        # pick the most appropriate paths for torrent and source path
        self.tr_path, self.src_path = self.__pick_path(args.fpaths, self.mode)
        # try loading user-defined preset for metadata
        self.metadata = self.__load_preset(args.preset, self.mode)
        # extract metadata from cli arguments
        self.metadata = self.__pick_metadata(args, self.mode, self.metadata)

    @staticmethod
    def __pick_cli_cfg(args):
        # if args.show_progress and 'tqdm' not in globals().keys():
        #     print("I: Progress bar won't be shown as not installed, consider `python3 -m pip install tqdm`.")
        #     args.show_progress = False
        cfg = namedtuple(
            'CFG', '     show_prompt       show_progress       with_time_suffix'
        )(args.show_prompt, args.show_progress, args.with_time_suffix)
        return cfg

    @staticmethod
    def __pick_mode(mode, f_paths):
        """Pick mode from paths is limited: some modes cannot be inferred."""
        if mode:

            if mode not in ('create', 'print', 'modify', 'verify'):
                Main.__exit('E: unexpected error in mode picker, please file a bug report.')

        else:  # mode == False

            if len(f_paths) == 1:
                if f_paths[0].is_dir_() or f_paths[0].is_file_():  # 1:F/D -> c
                    mode = 'create'
                elif f_paths[0].is_torrent_():  # 1:T -> p
                    mode = 'print'
                else:
                    Main.__exit(f"E: You supplied '{f_paths[0]}' cannot suggest a working mode as it does not exist.")

            elif len(f_paths) == 2:
                # inferred as `create` mode requires 1 existing and 1 virtual path
                if f_paths[0].is_virtual_dir_() and f_paths[1].is_file_():  # 1:D(v) 2:F = c
                    mode = 'create'
                elif (f_paths[0].is_file_() or f_paths[0].is_dir_()) and f_paths[1].is_virtual_dir_():  # 1:F/D 2:D(v) = c
                    mode = 'create'
                # inferred as `verify` requires both paths existing
                elif f_paths[0].is_torrent_() and (f_paths[1].is_file_() or f_paths[1].is_dir_()):  # 1:T 2:F/D = v
                    mode = 'verify'
                elif (f_paths[0].is_file_() or f_paths[0].is_dir_()) and f_paths[1].is_torrent_():  # 1:F/D 2:T = v
                    mode = 'verify'
                else:
                    Main.__exit(f"E: You supplied '{f_paths[0]}' and '{f_paths[1]}' cannot suggest a working mode.")

            else:
                Main.__exit(f"E: Expect 1 or 2 positional paths, not {len(f_paths)}.")

        print(f"I: Working mode is '{mode}'.")
        return mode

    @staticmethod
    def __pick_path(f_paths, mode):
        """Based on the working mode, sort out the most proper paths for torrent and content."""
        src_path = None  # Source PATH is the path to the files specified by a torrent
        tr_path = None  # Torrent PATH is the path to the torrent itself

        # `create` mode requires 1 or 2 paths
        # spath must exist, while tr_path can be virtual
        if mode == 'create':
            if len(f_paths) == 1:
                if f_paths[0].exists():  # 1:F/D/T
                    src_path = f_paths[0]
                    tr_path = src_path.parent.joinpath(f"{src_path.name}.torrent")
                else:
                    Main.__exit(f"E: The source path '{f_paths[0]}' does not exist.")
            elif len(f_paths) == 2:
                if f_paths[0].is_virtual_dir_() and f_paths[1].is_file_():  # 1:D(v) 2:F
                    src_path = f_paths[1]
                    tr_path = f_paths[0].joinpath(f"{src_path.name}.torrent")
                elif (f_paths[0].is_dir_() or f_paths[0].is_file_()) and f_paths[1].is_virtual_dir_():  # 1:F/D 2:D(v)
                    src_path = f_paths[0]
                    tr_path = f_paths[1].joinpath(f"{src_path.name}.torrent")
                elif f_paths[0].is_virtual_torrent_() and (f_paths[1].is_dir_() or f_paths[1].is_file_()):  # 1:T(v) 2:F/D
                    src_path = f_paths[1]
                    tr_path = f_paths[0]
                elif (f_paths[0].is_dir_() or f_paths[0].is_file_()) and f_paths[1].is_virtual_torrent_():  # 1:F/D 2:T(v)
                    src_path = f_paths[0]
                    tr_path = f_paths[1]
                elif f_paths[0].is_torrent_() and f_paths[1].is_virtual_torrent_():  # 1:T 2:T(v)
                    src_path = f_paths[0]
                    tr_path = f_paths[1]
                elif f_paths[0].is_virtual_torrent_() and f_paths[1].is_torrent_():  # 1:T(v) 2:T
                    src_path = f_paths[1]
                    tr_path = f_paths[0]
                else:
                    Main.__exit('E: You supplied paths cannot work in `create` mode.')
            else:
                Main.__exit(f"E: `create` mode expects 1 or 2 paths, not {len(f_paths)}.")
            if src_path == tr_path:  # stop 1:T=2:T
                Main.__exit('E: Source and torrent path cannot be same.')
            if src_path.is_file() and src_path.suffix.lower() == '.torrent':  # warn spath:T
                print('W: You are likely to create torrent from torrent, which may be unexpected.')

        # `print` mode requires exactly 1 path
        # the path must be an existing tr_path
        elif mode == 'print':
            if len(f_paths) == 1:
                if f_paths[0].is_torrent_():
                    tr_path = f_paths[0]
                else:
                    Main.__exit(f"E: `print` mode expects a valid torrent path, not {f_paths[0]}.")
            else:
                Main.__exit(f"E: `print` mode expects exactly 1 path, not {len(f_paths)}.")

        # `verify` mode requires exactly 2 paths
        # inferred as `verify` requires both paths existing
        elif mode == 'verify':
            if len(f_paths) == 2:
                if f_paths[0].is_torrent_() and (f_paths[1].is_file_() or f_paths[1].is_dir_()):
                    src_path = f_paths[1]
                    tr_path = f_paths[0]
                elif (f_paths[0].is_file_() or f_paths[0].is_dir_()) and f_paths[1].is_torrent_():
                    src_path = f_paths[0]
                    tr_path = f_paths[1]
                else:
                    Main.__exit('E: `verify` mode expects a pair of valid source and torrent paths, but not found.')
            else:
                Main.__exit(f"E: `verify` mode expects exactly 2 paths, not {len(f_paths)}.")

        # `modify` mode requires 1 or 2 paths
        elif mode == 'modify':
            if 1 <= len(f_paths) <= 2:
                if f_paths[0].is_torrent_():
                    src_path = f_paths[0]
                    tr_path = src_path if not f_paths[1:] else (
                        f_paths[1].joinpath(src_path.name) if f_paths[1].is_dir() else (
                            f_paths[1] if f_paths[1].suffix.lower() == '.torrent' else
                                f_paths[1].parent.joinpath(f"{f_paths[1].name}.torrent")))
                    if src_path == tr_path:
                        print('W: You are likely to overwrite the source torrent, which may be unexpected.')
                else:
                    Main.__exit(f"E: `modify` mode expects a valid torrent path, not {f_paths[0]}.")
            else:
                Main.__exit(f"E: `modify` mode expects 1 or 2 paths, not {len(f_paths)}.")

        else:
            Main.__exit('E: Unexpected point reached in path picker, please file a bug report.')

        return tr_path, src_path

    @staticmethod
    def __load_preset(path, mode):
        metadata = dict()
        if mode != 'create':
            return metadata

        # prepare a preset candidate to read
        preset_path = None
        if path:
            preset_path = Path(path).absolute()
            if not preset_path.is_file():
                Main.__exit(f"The preset file '{path}' does not exist.")
            if preset_path.suffix not in ('.json', '.torrent'):
                Main.__exit(f"E: Expect json or torrent to read presets, not '{path}'.")
        else:
            exec_path = sys.executable if getattr(sys, 'frozen', False) else __file__
            for ext in ('.json', '.torrent'):
                if (_ := Path(exec_path).absolute().with_suffix(ext)).is_file():
                    preset_path = _
                    break

        # try read the preset file
        if preset_path:
            try:
                print(f"I: Loading user presets from '{preset_path}'...", end=' ', flush=True)
                d = Torrent()
                if preset_path.suffix == '.torrent':
                    d.read(preset_path)
                elif preset_path.suffix == '.json':
                    d = json.loads(preset_path.read_bytes())
                else:
                    Main.__exit('E: Unexpected point reached in loading preset, please file a bug report.')

                if tracker_list := d.get('tracker_list'):
                    if isinstance(tracker_list, list) and all(isinstance(i, str) for i in tracker_list):
                        metadata['tracker_list'] = tracker_list
                    else:
                        print('W: tracker list is not loaded as incorrect format.')
                if d.get('comment'): metadata['comment'] = str(d.get('comment'))
                if d.get('created_by'): metadata['created_by'] = str(d.get('created_by'))
                if d.get('creation_date'):
                    if preset_path.suffix == '.torrent':
                        pass  # don't copy date if preset is a torrent file
                    else:
                        metadata['creation_date'] = int(d.get('creation_date'))
                if d.get('encoding'): metadata['encoding'] = str(d.get('encoding'))
                if d.get('piece_size'):
                    metadata['piece_size'] = int(d.get('piece_size'))
                    if preset_path.suffix != '.torrent':
                        metadata['piece_size'] = int(d.get('piece_size')) << 10
                if d.get('private'): metadata['private'] = int(d.get('private'))
                if d.get('source'): metadata['source'] = str(d.get('source'))
            except FileNotFoundError:
                Main.__exit('failed (file not found)')
            except UnicodeDecodeError:
                Main.__exit('failed (invalid file)')
            except json.decoder.JSONDecodeError:
                Main.__exit('failed (invalid file)')
            except BdecodeError:
                Main.__exit('failed (invalid file)')
            except KeyError:
                Main.__exit('failed (missing key)')
            else:
                print('succeeded')

        return metadata

    @staticmethod
    def __pick_metadata(args, mode, metadata):

        if mode == 'create':
            metadata['tracker_list'] = args.tracker_list if args.tracker_list else (
                _ if (_ := metadata.get('tracker_list')) else [])
            metadata['comment'] = args.comment if args.comment else (
                _ if (_ := metadata.get('comment')) else '')
            metadata['created_by'] = args.created_by if args.created_by else (
                _ if (_ := metadata.get('created_by')) else 'https://github.com/airium/TorrentUtils')
            metadata['creation_date'] = args.creation_date if args.creation_date else (
                _ if (_ := metadata.get('creation_date')) else int(time.time()))
            metadata['encoding'] = args.encoding if args.encoding else (
                _ if (_ := metadata.get('encoding')) else 'UTF-8')
            metadata['piece_size'] = args.piece_size << 10 if args.piece_size else (
                _ if (_ := metadata.get('piece_size')) else 4096 << 10)  # B -> KiB
            metadata['private'] = args.private if args.private else (
                _ if (_ := metadata.get('private')) else 0)
            metadata['source'] = args.source if args.source else (
                _ if (_ := metadata.get('source')) else '')

        elif mode == 'modify':
            if not (args.tracker_list is None): metadata['tracker_list'] = args.tracker_list
            if not (args.comment is None): metadata['comment'] = args.comment
            if not (args.created_by is None): metadata['created_by'] = args.created_by
            if not (args.creation_date is None): metadata['creation_date'] = args.creation_date
            if not (args.encoding is None): metadata['encoding'] = args.encoding
            if not (args.piece_size is None):
                print('W: supplied piece size has no effect in `modify` mode.')
                if 'piece_size' in metadata.keys():  # if piece_size is loaded from json, remove it
                    metadata.pop('piece_size')
            if not (args.private is None): metadata['private'] = args.private
            if not (args.source is None): metadata['source'] = args.source

        else:  # `print` or `verify`
            if not (args.tracker_list is None): print(f"W: supplied tracker has not effect in {mode} mode.")
            if not (args.comment is None): print(f"W: supplied comment has not effect in {mode} mode.")
            if not (args.created_by is None): print(f"W: supplied creator has not effect in {mode} mode.")
            if not (args.creation_date is None): print(f"W: supplied time has not effect in {mode} mode.")
            if not (args.encoding is None): print(f"W: supplied encoding has not effect in {mode} mode.")
            if not (args.piece_size is None): print(f"W: supplied piece size has not effect in {mode} mode.")
            if not (args.private is None): print(f"W: supplied private attribute has not effect in {mode} mode.")
            if not (args.source is None): print(f"W: supplied source has not effect in {mode} mode.")

        return metadata

    @staticmethod
    def __exit(chars=''):
        input(chars + '\nTerminated. (Press ENTER to exit)')
        sys.exit()

    def __prompt(self, chars):
        if (not self.cfg.show_prompt) or input(chars).lower() in ('y', 'yes'):
            return True
        else:
            return False

    def __call__(self):
        if self.mode == 'create':
            print(f"I: Creating torrent from '{self.src_path}'.")
            self._set()
            self._load()
            self._write()
        elif self.mode == 'print':
            self._read()
            self._print()
        elif self.mode == 'verify':
            print('I: Verifying Source files with Torrent.')
            print(f"Source: '{self.src_path}'")
            print(f"Torrent: '{self.tr_path}'")
            self._read()
            self._verify()
        elif self.mode == 'modify':
            print(f"I: Modifying torrent '{self.src_path}'.")
            self._read()
            self._set()
            self._write()
        else:
            self.__exit(f"Invalid mode: {self.mode}.")

        print()
        input('Press ENTER to exit...')

    def _print(self):
        tr_name = self.torrent.name
        tr_size = self.torrent.torrent_size
        tr_encoding = self.torrent.encoding
        tr_hash = self.torrent.hash
        f_size = self.torrent.size
        f_num = len(self.torrent.file_list)
        p_size = self.torrent.piece_length >> 10
        p_num = self.torrent.num_pieces
        tr_date = time.strftime('%Y/%m/%d %H:%M:%S', time.localtime(self.torrent.creation_date)) \
            if self.torrent.creation_date else ''
        tr_from = self.torrent.created_by if self.torrent.created_by else ''
        tr_private = 'Private' if self.torrent.private else 'Public'
        tr_src = self.torrent.source
        tr_comm = self.torrent.comment

        width = shutil.get_terminal_size()[0]

        print(f'General Info ' + '-' * (width - 14))
        print(f"Name: {tr_name}")
        print(f"File: {tr_size:,} Bytes, Bencoded" + (f" with {tr_encoding}" if tr_encoding else ''))
        print(f"Hash: {tr_hash}")
        print(f"Size: {f_size:,} Bytes" + f", {f_num} File" + ('s' if f_num > 1 else '') + f", {p_size} KiB x {p_num} Pieces")
        if tr_date and tr_from:
            print(f"Time: {tr_date} by {tr_from}")
        elif tr_date:
            print(f"Time: {tr_date}")
        elif tr_from:
            print(f"From: {tr_date}")
        if tr_comm:
            print(f"Comm: {tr_comm}")
        print(f"Else: {tr_private} torrent" + (f" by {tr_src}" if tr_src else ''))

        print(f'Trackers ' + '-' * (width - 10))
        if self.torrent.tracker_list:
            tracker_num = math.ceil(math.log10(len(self.torrent.tracker_list))) if self.torrent.tracker_list else 0
            for i, url in enumerate(self.torrent.tracker_list, start=1):
                print(f'{i:0>{tracker_num}}: {url}')
        else:
            print('No tracker')

        print(f'Files ' + '-' * (width - 7))
        if f_num == 1:
            print(f'1: {tr_name}')
        else:
            f_num = math.ceil(math.log10(f_num)) if f_num else 0
            for i, (f_size, fpath) in enumerate(self.torrent.file_list, start=1):
                print(f'{i:0>{f_num}}: {os.path.join(fpath[0], *fpath[1:])} ({f_size:,} bytes)')
                if i == 500 and self.cfg.show_prompt:
                    print('Truncated at 500 files (use -y/--yes to list all)')
                    break

    def _load(self):
        try:
            self.torrent.load(self.src_path, False, self.cfg.show_progress)
        except EmptySourceSize:
            self.__exit(f"The source path '{self.src_path.absolute()}' has a total size of 0.")

    def _read(self):
        if self.mode in ('verify', 'print'):
            self.torrent.read(self.tr_path)
        elif self.mode == 'modify':
            self.torrent.read(self.src_path)
        else:
            self.__exit(f"Unexpected {self.mode} mode for read operation.")

    def _verify(self):
        src_path = self.src_path
        tr_name = self.torrent.name

        if self.torrent.num_files == 1:
            if src_path.is_file() and src_path.name == tr_name:
                src_path = self.src_path
            elif src_path.is_dir():
                if Path(tr_name) in src_path.iterdir() and (tmp := src_path.joinpath(tr_name)).is_file():
                    src_path = tmp
                else:
                    self.__exit(f"E: The source file '{src_path}' was not found.")
        elif self.torrent.num_files > 1:
            if src_path.is_file():
                self.__exit(f"E: The source directory '{src_path}' was not found.")
            elif src_path.is_dir():
                if src_path.name == tr_name:
                    src_path = src_path
                elif Path(tr_name) in src_path.iterdir() and (tmp := src_path.joinpath(tr_name)).is_dir():
                    src_path = tmp
                else:
                    self.__exit(f"E: The source directory '{src_path}' was not found.")

        piece_broken_list = self.torrent.verify(src_path)
        piece_total = self.torrent.num_pieces
        piece_broken = len(piece_broken_list)
        piece_passed = piece_total - piece_broken

        files_broken_list = [self.torrent[i] for i in piece_broken_list]
        files_broken_list = list(dict.fromkeys(chain(*files_broken_list)))
        files_total = self.torrent.num_files
        files_broken = len(files_broken_list)
        files_passed = files_total - files_broken

        print('Processing...')
        print(f"Piece: {piece_total:>10d} total = {piece_passed:>10d} passed + {piece_broken:>10d} missing or broken")
        print(f"Files: {files_total:>10d} total = {files_passed:>10d} passed + {files_broken:>10d} missing or broken")
        if files_broken_list:
            print('Files missing or broken:')
            for i, fpath in enumerate(files_broken_list):
                print(src_path.parent.joinpath(fpath))
                if i == 49:
                    if files_broken > 50:
                        print('Truncated at 50 files - too many potential missing or broken files.')
                    break
            print('\nI: Some files may be in fact OK but cannot be verified as their neighbour files failed.')

    def _set(self):
        try:
            self.torrent.set(**self.metadata)
        except PieceSizeTooSmall:
            self.__exit(f"Piece size must be larger than 16KiB, not {self.metadata['piece_size']} bytes.")
        except PieceSizeUncommon:
            if self.__prompt(f"Uncommon piece size {self.metadata['piece_size'] >> 10} KiB. Confirm? (y/N): "):
                self.torrent.set_piece_length(self.metadata['piece_size'], no_check=True)
                self.metadata.pop('piece_size')
                self.torrent.set(**self.metadata)
            else:
                self.__exit()

    def _write(self):
        f_path = self.tr_path.with_suffix(
            f"{'.' + time.strftime('%y%m%d-%H%M%S') if self.cfg.with_time_suffix else ''}.torrent")
        try:
            self.torrent.write(f_path, overwrite=False)
            print(f"I: Torrent saved to '{f_path}'.")
        except FileExistsError:
            if self.__prompt(f"The target file '{f_path}' already exists. Overwrite? (y/N): "):
                self.torrent.write(f_path, overwrite=True)
                print(f"I: Torrent saved to '{f_path}' (overwritten).")
            else:
                self.__exit()
        except IsADirectoryError:
            self.__exit(f"E: The target '{f_path}' is a directory.")
