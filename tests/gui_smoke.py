"""Optional desktop smoke check: synthetic data only; closes its own window."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]
WORKER = '''
import sys
sys.path.insert(0, sys.argv.pop(1))
import token_overlay as m
m.READERS = [lambda name=name: m.ProviderReading(name, 123, 1.5, "Synthetic smoke test", "ok") for name in m.PROVIDER_NAMES.values()]
Original = m.Overlay
class SmokeOverlay(Original):
    def __init__(self):
        super().__init__()
        print("READY", flush=True)
        self.root.after(1200, self.check_window)
        self.root.after(2000, self.quit)
    def check_window(self):
        self.update_readings([reader().__dict__ for reader in m.READERS])
        self.root.update_idletasks()
        assert all(row[1].cget("text") == "123" for row in self.rows.values())
        assert self.root.winfo_height() >= self.root.winfo_reqheight()
        self.show_collapsed()
        assert not self.expanded
        self.show_expanded()
        assert self.expanded
        self.hide()
        self.show_expanded()
        print("WINDOW_OK", flush=True)
m.Overlay = SmokeOverlay
m.main()
'''
with tempfile.TemporaryDirectory(prefix='overlay-gui-smoke-') as temp:
    env = dict(os.environ, XDG_RUNTIME_DIR=temp, TOKEN_OVERLAY_ENV='/nonexistent')
    first = subprocess.Popen([sys.executable, '-c', WORKER, str(PROJECT)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == 'READY'
        for args in ([], ['--show'], ['--refresh']):
            second = subprocess.run([sys.executable, str(PROJECT / 'token_overlay.py'), *args],
                                    env=env, capture_output=True, text=True, timeout=10)
            assert second.returncode == 0, second.stderr
        output, error = first.communicate(timeout=10)
        assert first.returncode == 0 and 'WINDOW_OK' in output and not error, (output, error)
        assert not list(Path(temp).rglob('control.sock'))
        print('PASS: actual Tk window, four rows, collapse/expand/hide/show, single instance, refresh, quit and socket cleanup')
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)
