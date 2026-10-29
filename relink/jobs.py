"""Job state, so `relink resume` can continue after a crash or reboot."""
import hashlib
import json
import os
import time
from pathlib import Path


def home():
    return Path(os.environ.get("RELINK_HOME") or Path.home() / ".relink")


def job_id(url, quality):
    return hashlib.sha1(f"{url}|{quality}".encode()).hexdigest()[:10]


def path_for(jid):
    return home() / "jobs" / f"{jid}.json"


def save(job):
    job["updated"] = int(time.time())
    p = path_for(job["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def load_all():
    d = home() / "jobs"
    jobs = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            jobs.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return sorted(jobs, key=lambda j: j.get("updated", 0))
