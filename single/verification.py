"""Full decoding validation; hardware acceleration never replaces validation."""
import subprocess
import time

from single.common import LocalError, NO_WINDOW


def command(path, hardware=False, sample=False):
    args = ['ffmpeg', '-nostdin', '-v', 'error', '-xerror']
    if hardware:
        args += ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda']
    args += ['-i', str(path)]
    if sample:
        args += ['-t', '3']
    return args + ['-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-']


def decode_file(path, facts, notify):
    started = time.monotonic()
    hardware = False
    if facts['duration'] >= 180 and (facts.get('width') or 0) * (facts.get('height') or 0) >= 1920 * 1080:
        try:
            probe = subprocess.run(command(path, hardware=True, sample=True), capture_output=True, timeout=15, creationflags=NO_WINDOW)
            hardware = probe.returncode == 0
        except subprocess.TimeoutExpired:
            pass
    if hardware:
        notify(status='verifying', message='正在使用 GPU 完整解码校验音画，未修改视频画质', percent=None)
        result = subprocess.run(command(path, hardware=True), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800, creationflags=NO_WINDOW)
        if result.returncode == 0:
            return {'decoder':'cuda', 'seconds':round(time.monotonic()-started,2), 'full_decode':True}
        notify(status='verifying', message='GPU 校验未通过，正在使用软件完整解码复核', percent=None)
    result = subprocess.run(command(path), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=1800, creationflags=NO_WINDOW)
    if result.returncode:
        raise LocalError('integrity', '视频完整解码检查失败，残缺文件未标记为成功。')
    return {'decoder':'software', 'seconds':round(time.monotonic()-started,2), 'full_decode':True}
