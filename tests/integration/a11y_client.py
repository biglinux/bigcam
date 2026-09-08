#!/usr/bin/env python3
"""Require BigCam in the real AT-SPI desktop; fail when the binding is absent."""
import json
import time
import pyatspi

def walk(node,depth=0):
    data={"name":node.name,"role":node.getRoleName(),"children":[]}
    if depth<12:
        for child in node:
            if child:data["children"].append(walk(child,depth+1))
    return data

end=time.monotonic()+12
while time.monotonic()<end:
    desktop=pyatspi.Registry.getDesktop(0)
    for node in desktop:
        if node and "bigcam" in node.name.lower():
            data=walk(node)
            assert data["children"],"Application has no accessible window"
            print(json.dumps(data,ensure_ascii=False,indent=2))
            raise SystemExit(0)
    time.sleep(.25)
raise SystemExit("BigCam did not expose an AT-SPI application")
