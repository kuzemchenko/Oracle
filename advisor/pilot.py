"""Temporary HTTPS pilot supervisor. Tunnel restart changes its public URL."""
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]

def main():
    os.umask(0o077)
    storage=ROOT/'storage';storage.mkdir(exist_ok=True)
    public_file=storage/'public_url.txt'
    public_file.unlink(missing_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    # The tunnel process must not inherit financial provider credentials.
    tunnel_env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(storage)}
    tunnel=subprocess.Popen([str(ROOT/'bin/cloudflared'),'tunnel','--no-autoupdate','--url',f'http://127.0.0.1:{port}'],
        env=tunnel_env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
    lines=queue.Queue(maxsize=1000)
    def reader():
        for line in tunnel.stderr:
            try:lines.put_nowait(line)
            except queue.Full:pass
    threading.Thread(target=reader,daemon=True).start()
    server=None
    def stop(*args):raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        deadline=time.monotonic()+90; origin=None
        while time.monotonic()<deadline and tunnel.poll() is None:
            try:line=lines.get(timeout=1)
            except queue.Empty:continue
            match=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',line)
            if match:origin=match.group(0);break
        if origin is None:raise RuntimeError('Tunnel URL unavailable; check outbound HTTPS/QUIC connectivity')
        env=dict(os.environ,ORACLE_PUBLIC_URL=origin,ORACLE_MCP_PORT=str(port))
        server=subprocess.Popen([sys.executable,'-m','advisor.server'],env=env,cwd=ROOT)
        import requests
        ready=False
        for _ in range(30):
            if server.poll() is not None:break
            try:
                r=requests.get(f'http://127.0.0.1:{port}/.well-known/oauth-authorization-server',timeout=1)
                ready=r.status_code==200 and r.json().get('issuer','').rstrip('/')==origin
            except (requests.RequestException,ValueError):pass
            if ready:break
            time.sleep(1)
        if not ready:raise RuntimeError('OAuth service did not become ready')
        public_file.write_text(origin+'/mcp\n')
        print('Oracle pilot ready; public URL is in storage/public_url.txt',flush=True)
        while tunnel.poll() is None and server.poll() is None:time.sleep(1)
        raise RuntimeError('Pilot stopped; restart changes URL and requires reconnecting ChatGPT')
    finally:
        public_file.unlink(missing_ok=True)
        for child in (server,tunnel):
            if child is not None and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait()

if __name__=='__main__':main()
