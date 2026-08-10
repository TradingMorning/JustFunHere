"""
Scheduler ko web app se bilkul alag, standalone process ke roop me chalate hain.
Isse guarantee milta hai ki web app (gunicorn/flask) me chahe kitne bhi
workers/processes chalein, scheduler sirf EK hi jagah, EK hi baar chalega —
isse "double upload" wala bug nahi hoga.

Run karne ka tarika:
    python run_scheduler.py

Isko hamesha background me alag se chalate rehna hai (web server se independent),
jaise ek dusra terminal, ya production me systemd/supervisor service ke roop me.
"""

import socket
import dns.resolver

_dns_cache = {}

def _use_google_dns(host, port, family=0, type=0, proto=0, flags=0):
    if host not in _dns_cache:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = ["1.1.1.1", "8.8.8.8"]
        try:
            answer = resolver.resolve(host, "A")
            _dns_cache[host] = answer[0].to_text()
        except Exception:
            _dns_cache[host] = host

    resolved_ip = _dns_cache[host]
    return socket._original_getaddrinfo(resolved_ip, port, family, type, proto, flags)

socket._original_getaddrinfo = socket.getaddrinfo
socket.getaddrinfo = _use_google_dns


import time
from services.scheduler import start_scheduler

scheduler = start_scheduler()
print("✅ Scheduler started successfully. Waiting for due videos...", scheduler, time)


# Process ko zinda rakhna zaroori hai, warna scheduler start hote hi
# is script ka kaam khatam ho jayega aur process turant exit ho jayega
# (aur scheduler bhi band ho jayega).
while True:
    time.sleep(60)