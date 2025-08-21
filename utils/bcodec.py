from typing import Any, List, Tuple, Union, Literal

from .errors import BdecodeError

StackItem = Union[
    Tuple[Literal['emit'], bytes],
    Tuple[Literal['proc'], Any],
]


def bencode(obj, encoding: str = 'utf-8') -> bytes:
    out: List[bytes] = []
    stack: List[StackItem] = [('proc', obj)]

    while stack:
        tag, payload = stack.pop()

        if tag == 'emit':
            out.append(payload)
            continue

        x = payload

        if isinstance(x, bytes):
            out.append(str(len(x)).encode(encoding))
            out.append(b':')
            out.append(x)

        elif isinstance(x, str):
            bx = x.encode(encoding)
            out.append(str(len(bx)).encode(encoding))
            out.append(b':')
            out.append(bx)

        elif isinstance(x, int):
            out.append(b'i' + str(x).encode(encoding) + b'e')

        elif isinstance(x, (list, tuple)):
            stack.append(('emit', b'e'))
            for elem in reversed(x):
                stack.append(('proc', elem))
            stack.append(('emit', b'l'))

        elif isinstance(x, dict):
            items = sorted(x.items())
            for k, _ in items:
                if not isinstance(k, (bytes, str)):
                    raise TypeError(f'Expect str or bytes, not {k}:{type(k)}.')
            stack.append(('emit', b'e'))
            for k, v in reversed(items):
                stack.append(('proc', v))
                stack.append(('proc', k))
            stack.append(('emit', b'd'))

        else:
            raise TypeError(f'Expect int, bytes, list or dict, not {x}:{type(x)}.')

    return b''.join(out)


def bdecode(b: bytes, encoding: str = 'ascii'):
    """Iterative bdecode."""
    b = b.encode(encoding) if isinstance(b, str) else b
    b_len = len(b)
    idx = 0

    stack: list[tuple[str, list]] = []  # Stack Frame: ('l'| 'd', items_list)
    root = None

    def push_value(value):
        nonlocal root
        if stack:
            stack[-1][1].append(value)
        else:
            if root is None:
                root = value
            else:
                raise BdecodeError('Malformed input.')

    while idx < b_len:
        t = b[idx]
        if t == ord(b'i'):  # integer: i<digits>e
            j = b.find(b'e', idx + 1)
            if j == -1:
                raise BdecodeError('Malformed input (unterminated integer).')
            try:
                num = int(b[idx + 1:j])
            except ValueError:
                raise BdecodeError('Malformed integer.')
            push_value(num)
            idx = j + 1

        elif t == ord(b'l'):  # list
            stack.append(('l', []))
            idx += 1

        elif t == ord(b'd'):  # dict
            stack.append(('d', []))
            idx += 1

        elif t == ord(b'e'):  # end of list/dict
            if not stack:
                raise BdecodeError('Malformed input (unexpected end).')
            typ, items = stack.pop()
            if typ == 'l':  # list
                val = items
            else:  # dict
                if len(items) % 2 != 0:
                    raise BdecodeError('Malformed dict (odd number of items).')
                val = {items[k]: items[k + 1] for k in range(0, len(items), 2)}
            push_value(val)
            idx += 1

        elif ord(b'0') <= t <= ord(b'9'):  # '0'...'9' => byte string: <len>:<payload>
            j = b.find(b':', idx)
            if j == -1:
                raise BdecodeError('Malformed string length.')
            try:
                length = int(b[idx:j])
            except ValueError:
                raise BdecodeError('Malformed string length.')
            start = j + 1
            end = start + length
            if end > b_len:
                raise BdecodeError('Malformed string (truncated).')
            push_value(b[start:end])
            idx = end

        else:
            raise BdecodeError('Malformed input (unknown token).')

    if stack:
        raise BdecodeError('Malformed input (unterminated list/dict).')
    if root is None:
        raise BdecodeError('Empty input.')
    return root
