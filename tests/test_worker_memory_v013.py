"""Synthetic procfs RSS accounting never returns command-line credentials."""
import json

import dreamina_api.managed_workers as module
from test_managed_workers import Harness,run


def test_memory_unsupported_platform_is_explicit(tmp_path,monkeypatch):
    h=Harness(tmp_path);monkeypatch.setattr(module.sys,'platform','unsupported')
    assert h.worker.browser_memory_snapshot()['available'] is False


def test_linux_memory_only_counts_owned_profile_browser_descendants(tmp_path,monkeypatch):
    h=Harness(tmp_path/'managed');run(h.worker.start('first'));root=tmp_path/'proc';root.mkdir()
    def process(pid,parent,argv,rss):
        directory=root/str(pid);directory.mkdir();fields=['0']*22
        fields[0]='S';fields[1]=str(parent);fields[19]=str(pid*10);fields[21]=str(rss)
        (directory/'stat').write_text(str(pid)+' (fixture) '+' '.join(fields));(directory/'cmdline').write_bytes(b'\0'.join(x.encode() for x in argv))
    process(1001,1,['python','fixture'],3)
    process(1002,1001,['node','playwright'],2)
    process(1003,1002,['/usr/bin/chromium','--user-data-dir='+str(h.worker.paths('first').profile),'--proxy-server=private'],10)
    process(1004,1003,['/usr/bin/chromium','--type=renderer'],20)
    process(1005,1,['/usr/bin/chromium','--user-data-dir='+str(h.worker.paths('first').profile)],90)
    process(1006,1001,['/usr/bin/chromium','--user-data-dir=/other-profile'],90)
    monkeypatch.setattr(module.sys,'platform','linux');monkeypatch.setattr(module.os,'sysconf',lambda _:4096,raising=False)
    report=h.worker.browser_memory_snapshot(_proc_root=root)
    assert report['available'] and report['browser_processes']==2 and report['rss_bytes']==30*4096
    assert report['accounts']==[{'account_id':'first','rss_bytes':30*4096,'browser_processes':2}]
    assert 'private' not in json.dumps(report) and 'cmdline' not in json.dumps(report) and 'argv' not in json.dumps(report)
