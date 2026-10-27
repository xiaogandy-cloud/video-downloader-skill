"""Probe the same staging-to-final operations used by real downloads."""
import errno
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from single.common import LocalError


def storage_error(exc):
    if isinstance(exc, PermissionError) or getattr(exc, 'winerror', None) == 5:
        return LocalError('storage_permission', '当前后台没有保存目录的写入权限。请选择可写目录；若由受限终端启动，请先关闭工具，再从资源管理器双击启动入口。不需要修改系统权限或关闭安全防护。')
    if getattr(exc, 'errno', None) == errno.ENOSPC or getattr(exc, 'winerror', None) in (39, 112):
        return LocalError('storage_space', '保存磁盘空间不足，请清理空间或选择其他磁盘，再重新下载。')
    if isinstance(exc, (FileExistsError, NotADirectoryError)):
        return LocalError('storage_path', '保存路径或临时目录被同名文件占用，请选择一个有效文件夹。')
    return LocalError('storage_io', '保存目录读写检查失败，可能是磁盘断开或文件被占用，请重新选择目录。')


def check_storage(value):
    if any(ord(c) < 32 for c in str(value)):
        raise LocalError('storage_path', '保存路径含有无效字符，请重新选择文件夹。')
    folder = Path(value).expanduser()
    if not folder.is_absolute() or str(folder).startswith('\\\\'):
        raise LocalError('storage_path', '请选择本机磁盘上的完整文件夹路径。')
    try:
        folder = folder.resolve()
    except OSError as exc:
        raise storage_error(exc) from exc
    if len(str(folder)) > 100:
        raise LocalError('storage_path', '保存目录路径过长，请选择完整路径不超过 100 个字符的目录。')
    stage = probe = published = None
    published_owned = False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        parts = folder / '.local-video-parts'
        parts.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='check-', dir=parts))
        probe = stage / 'write-test.tmp'
        published = folder / f'.local-video-{stage.name}.tmp'
        with probe.open('xb') as handle:
            handle.write(b'local-video-storage-check')
            handle.flush()
            os.fsync(handle.fileno())
        probe.rename(published)
        published_owned = True
        published.unlink()
        stage.rmdir()
        free = shutil.disk_usage(folder).free
        if free < 16 * 1024 * 1024:
            raise LocalError('storage_space', '保存磁盘可用空间不足 16 MB，请清理空间或选择其他磁盘。')
        return {'ok': True, 'path': str(folder), 'free_bytes': free,
                'checked_at': datetime.now().astimezone().isoformat(timespec='seconds')}
    except OSError as exc:
        raise storage_error(exc) from exc
    finally:
        # Only remove this probe's exact files and empty directory, never user media.
        for path in (probe, published if published_owned else None):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        if stage is not None:
            try:
                stage.rmdir()
            except OSError:
                pass


def storage_status(value):
    try:
        return check_storage(value)
    except LocalError as exc:
        return {'ok': False, 'path': str(value), 'code': exc.code, 'message': str(exc)}
