from __future__ import annotations

import copy
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from single.common import DATA, ROOT, NO_WINDOW, NAMES, LocalError, atomic_json, final_destination, read_json, single_url, error_message
from single.storage import check_storage, storage_error

ACTIVE = {'parsing', 'downloading', 'merging', 'verifying', 'awaiting', 'cancelling'}


def stop_tree(process):
    if process and process.poll() is None:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=12, creationflags=NO_WINDOW)
        else:
            process.terminate()
        with __import__('contextlib').suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


class TaskManager:
    def __init__(self, data=DATA, worker_command=None, parse_timeout=180, idle_timeout=120):
        self.data = Path(data)
        self.lock = threading.RLock()
        self.current = read_json(self.data / 'last-result.json', None)
        if self.current and self.current.get('status') in ACTIVE:
            self.current.update(status='failed', code='interrupted', message='上次程序退出时任务未完成，请重新下载。', path=None)
        self.process = None
        self.cancel_event = threading.Event()
        self.thread = None
        self.command = worker_command or [sys.executable, '-m', 'single.engine']
        self.parse_timeout = parse_timeout
        self.idle_timeout = idle_timeout
        self.storage = None

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.current)

    def active(self):
        return bool(self.current and self.current.get('status') in ACTIVE)

    def update(self, **values):
        with self.lock:
            self.current.update(values)

    def start(self, text, config):
        with self.lock:
            if self.active() or (self.thread and self.thread.is_alive()):
                raise LocalError('busy', '已有任务正在进行，请等待完成或取消。')
            url, platform, _ = single_url(text)
            if not config.get('folder_confirmed'):
                raise LocalError('folder', '首次使用请先选择或确认保存目录。')
            if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
                raise LocalError('local', '未找到 FFmpeg / FFprobe，请运行 setup.bat。')
            self.storage = check_storage(config['download_dir'])
            folder = Path(self.storage['path'])
            try:
                folder.mkdir(parents=True, exist_ok=True)
                job_id = uuid.uuid4().hex
                stage = folder / '.local-video-parts' / job_id
                stage.mkdir(parents=True)
            except OSError as exc:
                raise storage_error(exc) from exc
            self.current = dict(id=job_id, platform=platform.value, platform_name=NAMES[platform.value], status='parsing', message='正在启动解析', percent=None, title='', path=None, watermark='水印情况待确认')
            try:
                atomic_json(self.data / 'last-result.json', self.current)
            except OSError as exc:
                stage.rmdir()
                self.current.update(status='failed', code='local', message='无法保存任务状态，请检查工具目录权限。')
                raise LocalError('local', self.current['message']) from exc
            self.cancel_event.clear()
            self.thread = threading.Thread(target=self._run, args=(url, copy.deepcopy(config), stage), daemon=True)
            self.thread.start()
            return copy.deepcopy(self.current)

    def _run(self, url, config, stage):
        job_id = stage.name
        incoming = queue.Queue()
        started = time.monotonic()
        last_progress = started
        phase_start = started
        signature = None
        last_status = 'parsing'
        ready = None
        failed = False
        try:
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', '')
            env['YTDLP_NO_PLUGINS'] = '1'
            process = subprocess.Popen(self.command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', env=env, creationflags=NO_WINDOW)
            with self.lock:
                self.process = process
            process.stdin.write(json.dumps({'url': url, 'config': config, 'stage': str(stage), 'session_root': str(self.data / 'sessions')}, ensure_ascii=False) + '\n')
            process.stdin.flush()
            def read_events():
                for line in process.stdout:
                    try:
                        message = json.loads(line)
                        if isinstance(message, dict): incoming.put(message)
                    except ValueError:
                        continue
                incoming.put(None)
            reader = threading.Thread(target=read_events, daemon=True)
            reader.start()
            eof = False
            while not eof:
                if self.cancel_event.is_set():
                    stop_tree(process)
                    self.update(status='cancelled', message='已取消，未完成文件已清理', percent=None, path=None)
                    failed = True
                    break
                now = time.monotonic()
                state = self.snapshot()['status']
                limit = self.parse_timeout if state == 'parsing' else (600 if state in ('merging', 'awaiting') else 1800 if state == 'verifying' else self.idle_timeout)
                reference = phase_start if state in ('parsing', 'merging', 'awaiting', 'verifying') else last_progress
                if now - reference > limit or now - started > 7200:
                    raise LocalError('timeout', f'当前阶段超过 {limit} 秒未完成，已停止并清理临时文件。请检查网络或登录状态后重试。')
                try:
                    event = incoming.get(timeout=.15)
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive(): break
                    continue
                if event is None:
                    eof = True
                    continue
                state = event.get('status', last_status)
                if state != last_status:
                    phase_start = now
                    last_progress = now
                    last_status = state
                new_signature = (event.get('downloaded'), event.get('total'))
                if new_signature != signature and new_signature[0] is not None:
                    last_progress = now
                    signature = new_signature
                if state == 'ready':
                    ready = event
                elif state == 'failed':
                    self.update(**event, path=None, percent=None)
                    failed = True
                else:
                    self.update(**{k: v for k, v in event.items() if k in ('status', 'message', 'percent', 'speed', 'eta', 'downloaded', 'total', 'title', 'watermark')})
            process.wait(timeout=8)
            with self.lock:
                if self.cancel_event.is_set():
                    self.current.update(status='cancelled', message='已取消，未完成文件已清理', path=None, percent=None)
                elif not failed and ready and process.returncode == 0:
                    source = Path(ready['path']).resolve()
                    if not source.is_relative_to(stage.resolve()) or not source.is_file():
                        raise LocalError('integrity', '无法确认最终文件，下载未完成。')
                    destination = final_destination(config['download_dir'], self.current['platform'], ready['title'], source.suffix)
                    # Windows rename refuses to overwrite an existing destination.
                    source.rename(destination)
                    self.current.update(status='success', message='下载成功，完整音画校验通过', path=str(destination), filename=destination.name, facts=ready['facts'], validation=ready.get('validation'), title=ready['title'], watermark=ready['watermark'], percent=100)
                elif not failed:
                    raise LocalError('engine', '下载进程提前退出，未产生已校验的完整文件。')
        except Exception as exc:
            stop_tree(self.process)
            code, message = error_message(exc)
            self.update(status='failed', code=code, message=message, path=None, percent=None)
        finally:
            self.update(elapsed_seconds=round(time.monotonic() - started, 2))
            try:
                # Only the UUID directory created by this task is eligible for cleanup.
                expected = Path(config['download_dir']).resolve() / '.local-video-parts' / job_id
                if stage.resolve() == expected and stage.exists():
                    shutil.rmtree(stage)
                session_dir = (self.data / 'sessions' / job_id).resolve()
                session_root = (self.data / 'sessions').resolve()
                if session_dir.parent == session_root and session_dir.is_dir():
                    shutil.rmtree(session_dir)
            except OSError:
                self.update(cleanup_warning='部分临时文件被占用，未标记成功；关闭工具后可删除保存目录中的 .local-video-parts。')
            with self.lock:
                self.process = None
                try:
                    atomic_json(self.data / 'last-result.json', self.current)
                except OSError:
                    self.current['cleanup_warning'] = '本次状态未能保存到磁盘，请记录完整视频路径。'

    def cancel(self):
        with self.lock:
            if self.active():
                self.cancel_event.set()
                self.current.update(status='cancelling', message='正在取消并清理临时文件')
            return copy.deepcopy(self.current)

    def proceed(self):
        with self.lock:
            if not self.current or self.current['status'] != 'awaiting' or not self.process:
                raise LocalError('state', '当前没有待确认的有水印视频源。')
            self.process.stdin.write('continue\n')
            self.process.stdin.flush()
            self.current.update(status='downloading', message='已同意下载有水印源')

    def shutdown(self):
        self.cancel()
        if self.thread:
            self.thread.join(timeout=20)
        stop_tree(self.process)
