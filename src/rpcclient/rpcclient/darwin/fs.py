from typing import List, Mapping

from rpcclient.common import path_to_str
from rpcclient.exceptions import BadReturnValueError
from rpcclient.fs import Fs, DirEntry, ScandirIterator
from rpcclient.darwin.structs import dirent32, dirent64, stat64, statfs64


def do_stat(client, stat_name, filename: str):
    """ stat(filename) at remote. read man for more details. """
    with client.safe_malloc(stat64.sizeof()) as buf:
        err = client.symbols[stat_name](filename, buf)
        if err != 0:
            raise BadReturnValueError(f'failed to stat(): {filename}')
        return stat64.parse_stream(buf)


class DarwinDirEntry(DirEntry):
    def _fetch_stat(self, follow_symlinks):
        stat_name = 'stat64' if follow_symlinks else 'lstat64'
        return do_stat(self._client, stat_name, self.path)


class DarwinScandirIterator(ScandirIterator):
    def __iter__(self):
        dirent = dirent32
        if self._client.inode64:
            dirent = dirent64

        while True:
            self._client.errno = 0
            direntp = self._client.symbols.readdir(self._dirp)
            if not direntp:
                self.deallocate()
                if self._client.errno:
                    raise BadReturnValueError(f'Failed scanning dir: {self._client.last_error}')
                break
            entry = dirent.parse_stream(direntp)
            if entry.d_name in ('.', '..'):
                continue
            yield DarwinDirEntry(self.path, entry, self._client)

    def _deallocate(self):
        self._client.symbols.closedir(self._dirp)


class DarwinFs(Fs):
    @path_to_str('path')
    def stat(self, path: str):
        """ stat(filename) at remote. read man for more details. """
        return do_stat(self._client, 'stat64', path)

    @path_to_str('path')
    def scandir(self, path: str = '.'):
        dp = self._client.symbols.opendir(path)
        if not dp:
            raise BadReturnValueError(f'failed to opendir(): {path} ({self._client.last_error})')
        return DarwinScandirIterator(path, dp, self._client)

    @path_to_str('path')
    def setxattr(self, path: str, name: str, value: bytes):
        """ set an extended attribute value """
        count = self._client.symbols.setxattr(path, name, value, len(value), 0, 0).c_int64
        if count == -1:
            raise BadReturnValueError(f'failed to setxattr(): {path} ({self._client.last_error})')

    @path_to_str('path')
    def removexattr(self, path: str, name: str):
        """ remove an extended attribute value """
        count = self._client.symbols.removexattr(path, name, 0).c_int64
        if count == -1:
            raise BadReturnValueError(f'failed to removexattr(): {path} ({self._client.last_error})')

    @path_to_str('path')
    def listxattr(self, path: str) -> List[str]:
        """ list extended attribute names """
        max_buf_len = 1024
        with self._client.safe_malloc(max_buf_len) as xattributes_names:
            count = self._client.symbols.listxattr(path, xattributes_names, max_buf_len, 0).c_int64
            if count == -1:
                raise BadReturnValueError(f'failed to listxattr(): {path} ({self._client.last_error})')
            return [s.decode() for s in xattributes_names.peek(count).split(b'\x00')[:-1]]

    @path_to_str('path')
    def getxattr(self, path: str, name: str) -> bytes:
        """ get an extended attribute value """
        max_buf_len = 1024
        with self._client.safe_malloc(max_buf_len) as value:
            count = self._client.symbols.getxattr(path, name, value, max_buf_len, 0, 0).c_int64
            if count == -1:
                raise BadReturnValueError(f'failed to getxattr(): {path} ({self._client.last_error})')
            return value.peek(count)

    @path_to_str('path')
    def dictxattr(self, path: str) -> Mapping[str, bytes]:
        """ get a dictionary of all extended attributes """
        result = {}
        for k in self.listxattr(path):
            result[k] = self.getxattr(path, k)
        return result

    @path_to_str('path')
    def statfs(self, path: str):
        with self._client.safe_malloc(statfs64.sizeof()) as buf:
            if 0 != self._client.symbols.statfs64(path, buf):
                raise BadReturnValueError(f'statfs failed for: {path}')
            return statfs64.parse_stream(buf)
