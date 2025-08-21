import codecs
import hashlib
import math
import os
import pathlib
import re
import time
from itertools import chain
from operator import methodcaller
from urllib.parse import quote

import tqdm

from .bcodec import bencode, bdecode
from .errors import PieceSizeTooSmall, PieceSizeUncommon, EmptySourceSize, TorrentNotReadyError


def hash_sha1(byte_chars: bytes, /) -> bytes:
    """Return the sha1 hash for the given bytes."""
    if isinstance(byte_chars, bytes):
        hasher = hashlib.sha1()
        hasher.update(byte_chars)
        return hasher.digest()
    else:
        raise TypeError(f"Expect bytes, not {type(byte_chars)}.")


class Torrent:

    def __init__(self, **kwargs):
        """Create an instance of an empty torrent. Supply any arguments supported by`self.set()` to init metadata.

        Currently, only the most common attributes are supported, including:
            at the 1st level of torrent:
                `announce`, `announce-list`, `comment`, `created by`, `creation data`, `encoding`, `info`
            and in the `info` key:
                `files`, `name`, `piece length`, `pieces`, `private`, `source`
        Any other attributes will be lost.
        """

        # internal attributes and their default values
        self._tracker_lst = list()  # for `announce` and `announce-list`
        self._comment_str = str()  # for `comment`
        self._creator_str = str()  # for `created by`
        self._datetime_int = 0  # for `creation date`
        self._enc4txt_str = 'UTF-8'  # for `encoding`
        self._src_path_lst = list()  # for `files`
        self._src_size_lst = list()  # for `length`
        self._tr_name_str = str()  # for `name`
        self._piece_len_int = 4096 << 10  # for `piece length`
        self._src_sha1_byte = bytes()  # for `pieces`
        self._private_int = 0  # for `private`
        self._tr_source_str = str()  # for `source`

        # metadata init
        self.set(**kwargs)

    """-----------------------------------------------------------------------------------------------------------------
    Basic properties that mimic keys in an actual torrent, providing a straightforward access (except `info`).
    If the return value is `False`, the key does not exist.
    Note that `get()` method does not handle all of these properties.
    -----------------------------------------------------------------------------------------------------------------"""

    @property
    def announce(self) -> str:
        """Return the first tracker, or empty string if none."""
        return self._tracker_lst[0] if self._tracker_lst else ''

    @announce.setter
    def announce(self, url):
        """Set the first tracker."""
        assert isinstance(url, str), f"expect str, not {url.__class__.__name__}"
        self.set_tracker([url] + self.announce_list)  # `setTracker()` will deduplicate

    @property
    def announce_list(self) -> list:
        """Return all trackers if no less than 2, otherwise empty list."""
        return self._tracker_lst if len(self._tracker_lst) >= 2 else []

    @announce_list.setter
    def announce_list(self, urls):
        """Set the whole tracker list, must be no less than 2."""
        if len(urls) >= 2:
            self.set_tracker(urls)
        else:
            raise ValueError(f'Trackers supplied to announce-list must be no less than 2.')

    @property
    def comment(self) -> str:
        """Return the comment message, which can be displayed in various clients."""
        return self._comment_str

    @comment.setter
    def comment(self, chars):
        """Set the comment message."""
        self.set_comment(chars)

    @property
    def created_by(self) -> str:
        """Return the creator of the torrent."""
        return self._creator_str

    @created_by.setter
    def created_by(self, creator):
        """Set the creator of the torrent."""
        self.set_creator(creator)

    @property
    def creation_date(self) -> int:
        """Return torrent creation time, counted as the number of second since 1970-01-01."""
        return self._datetime_int

    @creation_date.setter
    def creation_date(self, date):
        """Set torrent creation time."""
        self.set_date(date)

    @property
    def encoding(self) -> str:
        """Return the encoding for text."""
        return self._enc4txt_str

    @encoding.setter
    def encoding(self, enc):
        """Set the encoding for text."""
        self.set_encoding(enc)

    @property
    def files(self) -> list:
        """Return the list of file size and path parts. Read-only."""
        return list([f_size, f_path.parts] for f_size, f_path in zip(self._src_size_lst, self._src_path_lst))

    @property
    def length(self) -> int:
        """Return the size of single file torrent (repel `files`). Read-only."""
        return self._src_size_lst[0] if len(self._src_size_lst) == 1 and len(self.files) == 0 else 0

    @property
    def name(self) -> str:
        """Return the root name of the torrent."""
        return self._tr_name_str

    @name.setter
    def name(self, name):
        """Set the root name of the torrent."""
        self.set_name(name)

    @property
    def piece_length(self) -> int:
        """Return the piece size in bytes."""
        return self._piece_len_int

    @piece_length.setter
    def piece_length(self, size):
        """Set the piece size in bytes."""
        self.set_piece_length(size)

    @property
    def pieces(self) -> bytes:
        """Return the long raw bytes of pieces' sha1. Read-only."""
        return self._src_sha1_byte

    @property
    def private(self) -> int:
        """Return 1 if the torrent is private, otherwise 0."""
        return 1 if self._private_int else 0

    @private.setter
    def private(self, private):
        """Set torrent private or not."""
        self.set_private(private)

    @property
    def source(self) -> str:
        """Return the special message particularly used by private trackers."""
        return self._tr_source_str

    @source.setter
    def source(self, src):
        """Set the special message, which is normally invisible in clients."""
        self.set_source(src)

    """-----------------------------------------------------------------------------------------------------------------
    Useful public torrent properties
    -----------------------------------------------------------------------------------------------------------------"""

    @property
    def tracker_list(self) -> list:
        """Unlike `announce_list`, always returns the full tracker list unconditionally."""
        return self._tracker_lst

    @tracker_list.setter
    def tracker_list(self, urls):
        """Set the whole tracker urls."""
        self.set_tracker(urls)

    @property
    def file_list(self) -> list:
        """Unlike `files` and `length`, always returns the full file size and paths unconditionally. Read-only."""
        return list([f_size, f_path.parts] for f_size, f_path in zip(self._src_size_lst, self._src_path_lst))

    @property
    def size(self) -> int:
        """Return the total size of all source files in the torrent. Read-only."""
        return sum(self._src_size_lst)

    @property
    def torrent_size(self) -> int:
        """Return the size of the torrent file itself (not source files). Read-only."""
        return len(bencode(self.torrent_dict, self.encoding))

    @property
    def num_pieces(self) -> int:
        """Return the total number of pieces within the torrent. Read-only."""
        return len(self._src_sha1_byte) // 20

    @property
    def num_files(self) -> int:
        """Return the total number of files within the torrent. Read-only."""
        return len(self.file_list)

    @property
    def hash(self) -> str:
        """Return the torrent hash at the moment. Read-only."""
        return hash_sha1(bencode(self.info_dict, self.encoding)).hex()

    @property
    def magnet(self) -> str:
        """Return the magnet link of the torrent. Read-only."""
        ret = f"magnet:?xt=urn:btih:{self.hash}"
        if self.name:
            ret += f"&dn={quote(self.name)}"
        if self.size:
            ret += f"&xl={self.size}"
        for url in self.tracker_list:
            ret += f"&tr={quote(url)}"
        return ret

    def get(self, key, ret=None):
        """Get various metadata with more flexible key aliases:

            tracker: t, tr, tracker, trackers, tl, trackerlist, announce, announces, announcelist
            comment: c, comm, comment, comments
            creator: b, by, createdby, creator, tool, creatingtool
            date: d, date, time, second, seconds, creationdate, creationtime, creatingdate, creatingtime
            encoding: e, enc, encoding, codec
            name: n, name, torrentname
            piece size: ps, pl, piecesize, piecelength
            private: p, private, privatetorrent, torrentprivate, pub, public, publictorrent, torrentpublic
            source: s, src, source
            filelist: fl, filelist
            size: ssz, sourcesize, sourcesz, size
            torrentsize: tsz, torrentsize, torrentsz
            numpieces: np, numpiece, numpieces
            numfiles: nf, numfile, numfiles
            hash: th, torrenthash, sha1, hash
            magnet: magnet, magnetlink, magneturl

        All alias are case-insensitive.
        All whitespaces and underscores will be stripped (e.g. dA_te == date).
        Same as calls to properties, this method does not raise error on key inexistence, but return None(default).
        """
        key = re.sub(r'[\s_]', '', key).lower()
        if key in ('t', 'tr', 'tracker', 'trackers', 'trackerlist', 'announce', 'announces', 'announcelist'):
            ret = self.tracker_list
        elif key in ('c', 'comment', 'comments'):
            ret = self.comment
        elif key in ('b', 'by', 'createdby', 'creator', 'tool', 'creatingtool'):
            ret = self.created_by
        elif key in ('d', 'date', 'time', 'second', 'seconds', 'creationdate', 'creationtime', 'creatingdate', 'creatingtime'):
            ret = self.creation_date
        elif key in ('e', 'enc', 'encoding', 'codec'):
            ret = self.encoding
        elif key in ('n', 'name', 'torrentname'):
            ret = self.name
        elif key in ('ps', 'pl', 'piecesize', 'piecelength'):
            ret = self.piece_length
        elif key in ('p', 'private', 'privatetorrent', 'torrentprivate'):
            ret = self.private
        elif key in ('pub', 'public', 'publictorrent', 'torrentpublic'):
            ret = not self.private
        elif key in ('s', 'src', 'source'):
            ret = self.source
        elif key in ('fl', 'filelist'):
            ret = self.file_list
        elif key in ('ssz', 'sourcesize', 'sourcesz', 'size'):
            ret = self.size
        elif key in ('tsz', 'torrentsize', 'torrentsz'):
            ret = self.torrent_size
        elif key in ('np', 'numpiece', 'numpieces'):
            ret = self.num_pieces
        elif key in ('nf', 'numfile', 'numfiles'):
            ret = self.num_files
        elif key in ('th', 'torrenthash', 'sha1', 'hash'):
            ret = self.hash
        elif key in ('magnet', 'magnetlink', 'magneturl'):
            ret = self.magnet

        return ret

    """-----------------------------------------------------------------------------------------------------------------
    The following properties does not support `get()` method
    -----------------------------------------------------------------------------------------------------------------"""

    @property
    def info_dict(self) -> dict:
        """Return the `info` dict of the torrent that affects hash. Read-only."""
        info_dict = {}
        if self.length:
            info_dict[b'length'] = self.length
        if self.files:
            info_dict[b'files'] = []
            for f_size, fpath_parts in self.files:
                info_dict[b'files'].append({b'length': f_size, b'path': fpath_parts})
        if self.name:
            info_dict[b'name'] = self.name
        if self.piece_length:
            info_dict[b'piece length'] = self.piece_length
        if self.pieces:
            info_dict[b'pieces'] = self.pieces
        if self.private:
            info_dict[b'private'] = self.private
        if self.source:
            info_dict[b'source'] = self.source
        return info_dict

    @property
    def torrent_dict(self) -> dict:
        """Return the complete dict of the torrent, ready to be bencoded and saved. Read-only."""
        torrent_dict = dict()

        # keys that not impact torrent hash
        if self.announce:
            torrent_dict[b'announce'] = self.announce
        if self.announce_list:
            torrent_dict[b'announce-list'] = list([url] for url in self.announce_list)
        if self.comment:
            torrent_dict[b'comment'] = self.comment
        if self.creation_date:
            torrent_dict[b'creation date'] = self.creation_date
        if self.created_by:
            torrent_dict[b'created by'] = self.created_by
        if self.encoding:
            torrent_dict[b'encoding'] = self.encoding

        # keys that impact torrent hash
        torrent_dict[b'info'] = self.info_dict

        # additional key to store the original hash
        torrent_dict[b'hash'] = self.hash

        return torrent_dict

    """-----------------------------------------------------------------------------------------------------------------
    Property setters
    -----------------------------------------------------------------------------------------------------------------"""

    def add_tracker(self, urls, /, top=True):
        """Add trackers.

        Arguments:
        urls: The tracker urls, can be a single string or an iterable of strings. Auto deduplicate.
        top: bool=True, place added trackers to the top if True, otherwise bottom.
        """
        urls = [urls] if isinstance(urls, str) else list(urls)
        if top:
            for url in urls[::-1]:  # we're appending left, so reverse it
                try:
                    idx = self._tracker_lst.index(url)
                except ValueError:  # not found, add it
                    self._tracker_lst.insert(0, url)
                else:  # found, remove the existing and push it to top
                    self._tracker_lst.pop(idx)
                    self._tracker_lst.insert(0, url)
        else:
            for url in urls:
                try:
                    _ = self._tracker_lst.index(url)
                except ValueError:  # not found, add it
                    self._tracker_lst.append(url)
                else:  # found, no need to update its position
                    pass

    def set_tracker(self, urls, /):
        """Set tracker list with the given urls, dropping all existing ones.

        Argument:
        urls: The tracker urls, can be a single string or an iterable of strings. Auto deduplicate.
        """
        urls = [urls] if isinstance(urls, str) else list(urls)
        self._tracker_lst.clear()
        self.add_tracker(urls)  # `addTracker() will deduplicate

    def rm_tracker(self, urls, /):
        """Remove tracker.

        Arguments:
        urls: The tracker urls, can be a single string or an iterable of strings.
        """
        urls = {urls} if isinstance(urls, str) else set(urls)
        for url in urls:
            try:
                idx = self._tracker_lst.index(url)
            except ValueError:
                continue  # not found, skip
            else:
                self._tracker_lst.pop(idx)  # found, remove it

    def set_comment(self, comment, /):
        """Set the comment message.

        Argument:
        comment: The comment message as str."""
        self._comment_str = str(comment)

    def set_creator(self, creator, /):
        """Set the creator of the torrent.

        Argument:
        creator: The str of the creator."""
        self._creator_str = str(creator)

    def set_date(self, date, /):
        """Set the time.

        Argument:
        date: Second since 1970-1-1 if int or float, `time.strptime()` format if str,
              time tuple or `time.struct_time` otherwise.
        """
        if isinstance(date, (int, float)):
            self._datetime_int = int(date)
        elif isinstance(date, str):
            self._datetime_int = int(time.mktime(time.strptime(date)))
        elif isinstance(date, time.struct_time):
            self._datetime_int = int(time.mktime(date))
        elif '__len__' in dir(date) and len(date) == 9:
            self._datetime_int = int(time.mktime((date[0], date[1], date[2], date[3], date[4], date[5], date[6], date[7], date[8])))
        else:
            raise ValueError('Supplied date is not understood.')

    def set_encoding(self, encoding, /):
        """Set the encoding for text.

        Argument:
        enc: The encoding, must be a valid one in python.
        """
        encoding = str(encoding)
        codecs.lookup(encoding)  # will raise LookupError if this encoding not exists
        self._enc4txt_str = encoding  # respect the encoding str supplied by user

    def set_name(self, name, /):
        """Set the root name. Note that this will prevent the torrent from hashing on the source files.

        Argument:
        name: The new root name."""
        name = str(name)
        if not name:
            raise ValueError('Torrent name cannot be empty.')
        if not all([False if (char in name) else True for char in r'\/:*?"<>|']):
            raise ValueError('Torrent name contains invalid character.')
        self._tr_name_str = name

    def set_piece_length(self, size, /, no_check=False):
        """Set torrent piece size.
        Note that changing piece size to a different value will clear existing torrent piece hash.
        Exception will be raised when the new piece size looks strange:
        1. the piece size divided by 16KiB does not obtain a power of 2.
        2. the piece size is beyond the range [256KiB, 32MiB].
        Piece size smaller than 16KiB is never allowed.

        Argument:
        size: the piece size in bytes
        no_check: bool=False, whether to allow uncommon piece size and bypass exceptions
        """
        size = int(size)
        no_check = bool(no_check)
        if size == self._piece_len_int:  # we have nothing to do
            return

        if size < 16384:  # piece size must be larger than 16KiB
            raise PieceSizeTooSmall()
        if (not no_check) and ((math.log2(size / 262144) % 1) or (size < 262144) or (size > 33554432)):
            raise PieceSizeUncommon()
        if size != self._piece_len_int:  # changing piece size will clear existing hash
            self._src_sha1_byte = bytes()
        self._piece_len_int = size

    def set_private(self, private, /):
        """Set torrent private or not.

        Argument:
        private: Any value that can be converted to `bool`; private torrent if `True`.
        """
        self._private_int = int(bool(private))

    def set_source(self, src, /):
        """Set the source message.

        Argument:
        src: The message text that can be converted to `str`.
        """
        self._tr_source_str = str(src)

    def set(self, **metadata):
        """Set various metadata with more flexible key aliases:

            tracker: t, tr, tracker, trackers, trackerlist, announce, announces, announcelist
            comment: c, comm, comment, comments
            creator: b, by, createdby, creator, tool, creatingtool
            date: d, date, time, second, seconds, creationdate, creationtime, creatingdate, creatingtime
            encoding: e, enc, encoding, codec
            name: n, name, torrentname
            piece size: ps, pl, piecesize, piecelength
            private: p, private, privatetorrent, torrentprivate, pub, public, publictorrent, torrentpublic
            source: s, src, source

        All alias are case-insensitive.
        All whitespaces and underscores will be stripped (e.g. dA_te == date).
        If equivalent keys are supplied multiple times, the last one takes effect.
        Note that the values of keys have the same requirement as each backend function.
        """
        for key, value in metadata.items():
            key = re.sub(r'[\s_]', '', key).lower()
            if key in ('t', 'tr', 'tracker', 'trackers', 'trackerlist', 'announce', 'announces', 'announcelist'):
                self.set_tracker(value)
            elif key in ('c', 'comment', 'comments'):
                self.set_comment(value)
            elif key in ('b', 'by', 'createdby', 'creator', 'tool', 'creatingtool'):
                self.set_creator(value)
            elif key in ('d', 'date', 'time', 'second', 'seconds', 'creationdate', 'creationtime', 'creatingdate', 'creatingtime'):
                self.set_date(value)
            elif key in ('e', 'enc', 'encoding', 'codec'):
                self.set_encoding(value)
            elif key in ('n', 'name', 'torrentname'):
                self.set_name(value)
            elif key in ('ps', 'pl', 'piecesize', 'piecelength'):
                self.set_piece_length(value)
            elif key in ('p', 'private', 'privatetorrent', 'torrentprivate'):
                self.set_private(value)
            elif key in ('pub', 'public', 'publictorrent', 'torrentpublic'):
                self.set_private(not value)
            elif key in ('s', 'src', 'source'):
                self.set_source(value)
            else:
                raise KeyError(f"Unknown key: {key}.")

    """-----------------------------------------------------------------------------------------------------------------
    Input/output operations
    -----------------------------------------------------------------------------------------------------------------"""

    def read(self, tr_path, /):
        """Load everything from the template. Note that this function will clear all existing properties.

        Argument:
        tr_path: the path to the torrent."""
        tr_path = pathlib.Path(tr_path)
        if not tr_path.is_file():
            raise FileNotFoundError(f"The supplied '{tr_path}' does not exist.")
        torrent_dict = bdecode(tr_path.read_bytes())

        # we need to know encoding first
        encoding = torrent_dict.get(b'encoding', b'UTF-8').decode()  # str

        # tracker list
        trackers = [torrent_dict[b'announce']] if torrent_dict.get(b'announce') else []
        trackers += list(chain(*torrent_dict[b'announce-list'])) if torrent_dict.get(b'announce-list') else []
        trackers = list(map(methodcaller('decode', encoding), trackers))  # bytes to str
        trackers = list(dict.fromkeys(trackers))  # ordered deduplicate

        # other keys
        comment = torrent_dict.get(b'comment', b'').decode(encoding)  # str
        created_by = torrent_dict.get(b'created by', b'').decode(encoding)  # str
        creation_date = torrent_dict.get(b'creation date', 0)  # int
        files = torrent_dict.get(b'info').get(b'files', [])  # list
        length = torrent_dict.get(b'info').get(b'length', 0)  # int
        name = torrent_dict.get(b'info').get(b'name', b'').decode(encoding)  # str
        piece_length = torrent_dict.get(b'info').get(b'piece length', 0)  # int
        pieces = torrent_dict.get(b'info').get(b'pieces', b'')  # str
        private = torrent_dict.get(b'info').get(b'private', 0)  # int
        source = torrent_dict.get(b'info').get(b'source', b'').decode(encoding)  # str

        # everything looks good, now let's write attributes
        self.set_tracker(trackers)
        self.set_comment(comment)
        self.set_creator(created_by)
        self.set_date(creation_date)
        self.set_encoding(encoding)
        self.set_name(name)
        self.set_piece_length(piece_length, no_check=True)
        self.set_private(private)
        self.set_source(source)

        self._src_sha1_byte = pieces
        if length and not files:
            self._src_path_lst = [pathlib.Path('.')]
            self._src_size_lst = [length]
        elif not length and files:
            f_size_list = []
            f_path_list = []
            for file in files:
                f_size_list.append(file[b'length'])
                f_path_list.append(pathlib.Path().joinpath(*map(methodcaller('decode', encoding), file[b'path'])))
            self._src_size_lst = f_size_list
            self._src_path_lst = f_path_list
        else:
            raise ValueError('Unexpected error in handling source files structure.')

    def read_metadata(self, tr_path, /, include_key=None, exclude_key=None):
        """Unlike `read()`, this only loads and overwrites selected properties:
            trackers, comment, created_by, creation_date, encoding, source

        Arguments:
        tr_path: the path to the torrent
        `include_key`: str or set of str, only these keys will be copied
            keys: {trackers, comment, created_by, creation_date, encoding, source} (default=all)
        `exclude_key`: str or set of str, these keys will not be copied (override `include_key`)
            keys: {trackers, comment, created_by, creation_date, encoding, source} (default='source')
        """
        if exclude_key is None:
            exclude_key = {'source'}
        if include_key is None:
            include_key = {}
        tr_path = pathlib.Path(tr_path)
        if not tr_path.is_file():
            raise FileNotFoundError(f"The supplied '{tr_path}' does not exist.")

        key_set = {'tracker', 'comment', 'created_by', 'creation_date', 'encoding', 'source'}
        include_key = {include_key} if isinstance(include_key, str) else (
            set(include_key) if include_key else key_set)
        exclude_key = {exclude_key} if isinstance(exclude_key, str) else set(exclude_key)
        if (not include_key.issubset(key_set)) or (not exclude_key.issubset(key_set)):
            raise KeyError('Invalid key supplied.')

        template = Torrent()
        template.read(tr_path)
        for key in include_key.difference(exclude_key):
            if key == 'tracker':
                self._tracker_lst.extend(template.tracker_list)
                continue
            elif key == 'comment' and template.comment:
                self._comment_str = template.comment
                continue
            elif key == 'created_by' and template.created_by:
                self._creator_str = template.created_by
                continue
            elif key == 'creation_date' and template.creation_date:
                self._datetime_int = template.creation_date
                continue
            elif key == 'encoding' and template.encoding:
                self._enc4txt_str = template.encoding
                continue
            elif key == 'source' and template.source:
                self._tr_source_str = template.source
                continue
            raise RuntimeError('Loop not correctly continued.')

    def load(self, src_path, keep_name=False, show_progress=False):
        """Load new file list and piece hash from the Source PATH (src_path).

        The following torrent keys will be overwritten on success:
            files, name (maybe preserved by `keep_name=True`), pieces

        Arguments:
        src_path: path-like objects, the source path to be loaded
        keep_name: bool=False, whether to keep the old torrent name
        show_progress: bool=False, whether to show a progress bar during loading, maybe removed in the future
        """
        # argument handler
        src_path = pathlib.Path(src_path)
        if not src_path.exists():
            raise FileNotFoundError(f"The supplied '{src_path}' does not exist.")
        keep_name = bool(keep_name)
        show_progress = bool(show_progress)

        f_paths = [src_path] if src_path.is_file() else sorted(filter(methodcaller('is_file'), src_path.rglob('*')))
        f_path_list = [fpath.relative_to(src_path) for fpath in f_paths]
        f_size_list = [fpath.stat().st_size for fpath in f_paths]
        if sum(f_size_list):
            if show_progress:  # TODO: stdout is dirty in core class method and should be moved out in the future
                sha1 = b''
                piece_bytes = bytes()
                pbar1 = tqdm.tqdm(total=sum(f_size_list), desc='Size', unit='B', unit_scale=True, ascii=True, dynamic_ncols=True)
                pbar2 = tqdm.tqdm(total=len(f_size_list), desc='File', unit='', ascii=True, dynamic_ncols=True)
                for f_path in f_paths:
                    with f_path.open('rb', buffering=0) as f_obj:
                        while read_bytes := f_obj.read(self.piece_length - len(piece_bytes)):
                            piece_bytes += read_bytes
                            if len(piece_bytes) == self.piece_length:
                                sha1 += hash_sha1(piece_bytes)
                                piece_bytes = bytes()
                            pbar1.update(len(read_bytes))
                        pbar2.update(1)
                sha1 += hash_sha1(piece_bytes) if piece_bytes else b''
                pbar1.close()
                pbar2.close()
            else:  # not show progress bar
                sha1 = b''
                piece_bytes = bytes()
                for f_path in f_paths:
                    with f_path.open('rb', buffering=0) as f_obj:
                        while read_bytes := f_obj.read(self.piece_length - len(piece_bytes)):
                            piece_bytes += read_bytes
                            if len(piece_bytes) == self.piece_length:
                                sha1 += hash_sha1(piece_bytes)
                                piece_bytes = bytes()
                sha1 += hash_sha1(piece_bytes) if piece_bytes else b''
        else:
            raise EmptySourceSize()

        # Everything looks good, let's update internal parameters
        self.name = self.name if keep_name else src_path.name
        self._src_path_lst = f_path_list
        self._src_size_lst = f_size_list
        self._src_sha1_byte = sha1

    def write(self, tr_path, overwrite=False):
        """Save the torrent to file.

        Arguments:
        tr_path: path-like object, the path to save the torrent.
            If supplied an existing dir, it will be saved under that dir.
        overwrite: bool=False, whether to overwrite if the target file already exists.
        """
        tr_path = pathlib.Path(tr_path)
        overwrite = bool(overwrite)
        if error := self.check():
            raise TorrentNotReadyError(f"The torrent is not ready to be saved: {error}.")

        fpath = tr_path.joinpath(f"{self.name}.torrent") if tr_path.is_dir() else tr_path
        if fpath.is_file() and not overwrite:
            raise FileExistsError(f"The target '{fpath}' already exists.")
        else:
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_bytes(bencode(self.torrent_dict, self.encoding))

    def verify(self, src_path):
        """Verify external source files with the internal torrent.

        Argument:
        src_path: the path to source files.

        Return:
        The piece index from 0 that failed to hash
        """
        src_path = pathlib.Path(src_path)
        if not src_path.exists():
            raise FileNotFoundError(f"The source path '{src_path}' does not exist.")
        if self.check():
            raise TorrentNotReadyError(f"The torrent is not ready for verification.")

        if self.num_files == 1:
            if src_path.is_file() and src_path.name == self.name:
                src_path = src_path
            elif src_path.is_dir():
                if os.path.isfile(os.path.join(src_path, self.file_list[0][1][0])) and src_path.name == self.name:
                    src_path = src_path
                else:
                    raise IsADirectoryError(f"Expect a single file, not a directory '{src_path}'.")
            else:
                raise RuntimeError('Unexpected Error.')
        elif self.num_files > 1:
            if src_path.is_file():
                raise NotADirectoryError(f"Expect a directory, not a single file '{src_path}'.")
            elif src_path.is_dir() and src_path.name == self.name:
                src_path = src_path
            else:
                raise RuntimeError('Unexpected Error.')
        else:
            raise RuntimeError('Unexpected Error.')

        piece_bytes = bytes()
        piece_idx = 0
        piece_error_list = []
        for f_size, f_path in self.file_list:
            dest_fpath = src_path.joinpath(*f_path)
            if dest_fpath.is_file():
                read_quota = min(f_size, dest_fpath.stat().st_size)  # we only need to load the smaller file size
                with dest_fpath.open('rb', buffering=0) as dest_f_obj:
                    while read_bytes := dest_f_obj.read(min(self.piece_length - len(piece_bytes), read_quota)):
                        piece_bytes += read_bytes
                        if len(piece_bytes) == self.piece_length:  # whole piece loaded
                            if hash_sha1(piece_bytes) != self.pieces[20 * piece_idx: 20 * piece_idx + 20]:  # sha1 mismatch
                                piece_error_list.append(piece_idx)
                            piece_idx += 1  # whole piece loaded, piece index increase
                            piece_bytes = bytes()  # whole piece loaded, clear existing bytes
                        if (read_quota := read_quota - len(read_bytes)) == 0:  # smaller file read
                            # we need to fill remaining bytes
                            piece_bytes += b'\0' * diff if (diff := f_size - dest_fpath.stat().st_size) > 0 else b''
                            break
            else:  # the file does not exist
                size = len(piece_bytes) + f_size
                n_empty_piece, piece_blank_shift = divmod(size, self.piece_length)
                piece_bytes = b'\0' * piece_blank_shift  # it should be OK to just replace existing piece_bytes by \0
                for _ in range(n_empty_piece):
                    piece_error_list.append(piece_idx)
                    piece_idx += 1
        if piece_bytes and hash_sha1(piece_bytes) != self.pieces[20 * piece_idx: 20 * piece_idx + 20]:  # remainder
            piece_error_list.append(piece_idx)

        return piece_error_list

    """-----------------------------------------------------------------------------------------------------------------
    Other helper properties and members
    -----------------------------------------------------------------------------------------------------------------"""

    def check(self):
        """Return the problems within the torrent."""
        ret = []
        if not self.name:
            ret.append('Torrent name has not been set.')
        if not self.piece_length:
            ret.append('Piece size cannot be 0.')
        if not self.file_list:
            ret.append('There is no source file within the torrent.')
        if not self.pieces:
            ret.append('Piece hash is empty.')
        if not self.size:
            ret.append('Torrent size is 0.')
        if self.piece_length * (self.num_pieces - 1) > self.size:
            ret.append('Too many pieces for content size.')
        if self.piece_length * self.num_pieces < self.size:
            ret.append('Too less pieces for content size.')
        try:
            codecs.lookup(self.encoding)
        except LookupError:
            ret.append(f"Invalid encoding {self.encoding}.")
        try:
            bencode(self.torrent_dict, self.encoding)
        except Exception as e:
            ret.append(f"Torrent bencoding failed ({e}).")
        return ret

    def index(self, filename, /, num=1):
        """Given filename, return its piece index.

        Arguments:
        filename: str, the filename to find.
            Path parts are matched backward. e.g. 'b/c' will match 'a/b/c' instead of 'b/c/a'.
        num: int=1, stop search when this number of files are found (find all when num <= 0).

        Return:
        A list of 3-element tuple of (path, start-index, end-index)
            Indexed in python style, from 0 to len-1, and [m, n+1] for items from m to n
        """
        f_parts = pathlib.Path(filename).parts
        num = int(num) if int(num) > 0 else 0
        if self.check():
            raise TorrentNotReadyError('Torrent is not ready for indexing.')

        ret = []
        loaded_size = 0
        for f_size, f_path in self.file_list:
            n_shorter = min(len(f_path), len(f_parts))
            if f_path[:-n_shorter - 1:-1] == f_parts[:-n_shorter - 1:-1]:
                ret.append([os.path.join(self.name, *f_path),
                            math.floor(loaded_size / self.piece_length),
                            math.ceil((loaded_size + f_size) / self.piece_length)])
                if (num := num - 1) == 0:
                    break
            loaded_size += f_size

        return ret

    def __getitem__(self, key):
        """Given piece index, return files associated with it."""
        if self.check():
            raise TorrentNotReadyError('Torrent is not ready to for item getter.')

        if isinstance(key, int):
            l_size = self.piece_length * (key if key >= 0 else self.num_pieces + key)
            h_size = l_size + self.piece_length
        elif isinstance(key, slice):
            if key.step in (1, None):
                l_size = self.piece_length * (key.start if key.start >= 0 else self.num_pieces + key.start)
                h_size = self.piece_length * (key.stop if key.stop >= 0 else self.num_pieces + key.stop)
            else:
                raise ValueError(f"Piece index step must be 1, not {key.step}.")
        else:
            raise TypeError(f"Expect int or slice, not {key.__class__}.")

        ret = []

        if l_size >= h_size or l_size >= self.size:
            return ret

        size = 0
        for f_size, f_path in self.file_list:
            size += f_size
            if size > l_size:
                ret.append(os.path.join(self.name, *f_path))
            if size >= h_size:
                break

        return ret
