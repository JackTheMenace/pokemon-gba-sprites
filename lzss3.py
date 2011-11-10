#!/usr/bin/env python3

import sys
from sys import stdin, stdout, stderr, exit
from os import SEEK_SET, SEEK_CUR, SEEK_END
from errno import EPIPE
from struct import pack, unpack

__all__ = ('decompress', 'decompress_file', 'decompress_bytes',
           'decompress_overlay', 'DecompressionError')

class DecompressionError(ValueError):
    pass

def bits(byte):
    return ((byte >> 7) & 1,
            (byte >> 6) & 1,
            (byte >> 5) & 1,
            (byte >> 4) & 1,
            (byte >> 3) & 1,
            (byte >> 2) & 1,
            (byte >> 1) & 1,
            (byte) & 1)

def decompress_raw_lzss10(f, decompressed_size, _overlay=False):
    """Decompress LZSS-compressed bytes. Returns a bytearray."""
    data = bytearray()

    if _overlay:
        disp_extra = 3
    else:
        disp_extra = 1

    def readbyte():
        return f.read(1)[0]
    def writebyte(b):
        data.append(b)
    def copybyte():
        writebyte(readbyte())
    def readshort():
        # big-endian
        a, b = f.read(2)
        return (a << 8) | b

    while len(data) < decompressed_size:
        b = readbyte()
        if b == 0:
            # dumb optimization
            for _ in range(8):
                copybyte()
                if decompressed_size <= len(data):
                    break
            continue
        flags = bits(b)
        for flag in flags:
            if flag == 0:
                copybyte()
            elif flag == 1:
                sh = readshort()
                count = (sh >> 0xc) + 3
                disp = (sh & 0xfff) + disp_extra

                for _ in range(count):
                    writebyte(data[-disp])
            else:
                raise ValueError(flag)

            if decompressed_size <= len(data):
                break

    if len(data) != decompressed_size:
        raise DecompressionError("decompressed size does not match the expected size")

    return data

def decompress_raw_lzss11(f, decompressed_size):
    """Decompress LZSS-compressed bytes. Returns a bytearray."""
    data = bytearray()

    def writebyte(b):
        data.append(b)
    def readbyte():
        return f.read(1)[0]
    def copybyte():
        data.append(readbyte())

    while len(data) < decompressed_size:
        b = readbyte()
        if b == 0:
            # dumb optimization
            for _ in range(8):
                copybyte()
            continue
        flags = bits(b)
        for flag in flags:
            if flag == 0:
                copybyte()
            elif flag == 1:
                b = readbyte()
                indicator = b >> 4

                if indicator == 0:
                    # 8 bit count, 12 bit disp
                    # indicator is 0, don't need to mask b
                    count = (b << 4)
                    b = readbyte()
                    count += b >> 4
                    count += 0x11
                elif indicator == 1:
                    # 16 bit count, 12 bit disp
                    count = ((b & 0xf) << 12) + (readbyte() << 4)
                    b = readbyte()
                    count += b >> 4
                    count += 0x111
                else:
                    # indicator is count (4 bits), 12 bit disp
                    count = indicator
                    count += 1

                disp = ((b & 0xf) << 8) + readbyte()
                disp += 1

                try:
                    for _ in range(count):
                        writebyte(data[-disp])
                except IndexError:
                    raise Exception(count, disp, len(data), sum(1 for x in it) )
            else:
                raise ValueError(flag)

            if decompressed_size <= len(data):
                break

    if len(data) != decompressed_size:
        raise DecompressionError("decompressed size does not match the expected size")

    return data


def decompress_overlay(f, out):
    # the compression header is at the end of the file
    f.seek(-8, SEEK_END)
    header = f.read(8)

    # decompression goes backwards.
    # end < here < start

    # end_delta == here - decompression end address
    # start_delta == decompression start address - here
    end_delta, start_delta = unpack("<LL", header)

    filelen = f.tell()

    padding = end_delta >> 0x18
    end_delta &= 0xFFFFFF
    decompressed_size = start_delta + end_delta

    f.seek(-end_delta, SEEK_END)

    data = bytearray()
    data.extend(f.read(end_delta - padding))
    data.reverse()

    #stdout.write(data.tostring())

    uncompressed_data = decompress_raw_lzss10(data, decompressed_size,
                                              _overlay=True)
    uncompressed_data.reverse()

    # first we write up to the portion of the file which was "overwritten" by
    # the decompressed data, then the decompressed data itself.
    # i wonder if it's possible for decompression to overtake the compressed
    # data, so that the decompression code is reading its own output...
    f.seek(0, SEEK_SET)
    out.write(f.read(filelen - end_delta))
    out.write(uncompressed_data)

def decompress(obj):
    """Decompress LZSS-compressed bytes or a file-like object.

    Shells out to decompress_file() or decompress_bytes() depending on
    whether or not the passed-in object has a 'read' attribute or not.

    Returns a bytearray."""
    if hasattr(obj, 'read'):
        return decompress_file(obj)
    else:
        return decompress_bytes(obj)


def decompress_file(f):
    """Decompress an LZSS-compressed file. Returns a bytearray.
    """
    header = f.read(4)
    if header[0] == 0x10:
        decompress_raw = decompress_raw_lzss10
    elif header[0] == 0x11:
        decompress_raw = decompress_raw_lzss11
    else:
        raise DecompressionError("not an lzss-compressed file")

    decompressed_size, = unpack("<L", header[1:] + b'\x00')

    return decompress_raw(f, decompressed_size)

def main(args=None):
    if args is None:
        args = sys.argv[1:]

    if '--overlay' in args:
        args.remove('--overlay')
        overlay = True
    else:
        overlay = False

    if len(args) < 1 or args[0] == '-':
        if overlay:
            print("Can't decompress overlays from stdin", file=stderr)
            return 2

        if hasattr(stdin, 'detach'):
            f = stdin.detach()
        else:
            f = stdin
    else:
        try:
            f = open(args[0], "rb")
        except IOError as e:
            print(e, file=stderr)
            return 2

    stdout = sys.stdout
    if hasattr(stdout, 'detach'):
        # grab the underlying binary stream
        stdout = stdout.detach()

    try:
        if overlay:
            decompress_overlay(f, stdout)
        else:
            stdout.write(decompress_file(f))
    except IOError as e:
        if e.errno == EPIPE:
            # don't complain about a broken pipe
            pass
        else:
            raise
    except (DecompressionError,) as e:
        print(e, file=stderr)
        return 1

    return 0



if __name__ == '__main__':
    exit(main())
