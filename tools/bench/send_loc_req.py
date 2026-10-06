"""Send one loc_req down envelope to a pager through the relay's own publish path.
Credentials are read from Secret Manager into this process's environment only."""

import os, subprocess, sys, time
def secret(name):
    return subprocess.run(["gcloud","secrets","versions","access","latest","--secret",name,"--project","example-project"],
                          capture_output=True, text=True, check=True).stdout.strip()
os.environ["GOOGLE_CLOUD_PROJECT"]="example-project"
os.environ["BROKER_API_URL"]="https://broker.example.com:8443/api/v5"
os.environ["BROKER_API_KEY"]=secret("BROKER_API_KEY")
os.environ["BROKER_API_SECRET"]=secret("BROKER_API_SECRET")
os.environ.setdefault("WEBHOOK_KEY","unused-here")
for k in ("FIRESTORE_EMULATOR_HOST","FIREBASE_AUTH_EMULATOR_HOST","DEV_MODE"):
    os.environ.pop(k, None)
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "relay")
)
from app.config import Settings
from app.broker import BrokerClient
from app.ids import new_message_id
device = sys.argv[1]
obj={"v":1,"id":new_message_id(),"ts":int(time.time()),"kind":"loc_req","from":"claude","ack":None}
ok=BrokerClient(Settings.from_env()).publish_down(device, obj)
print(time.strftime("%H:%M:%S", time.gmtime()), "sent" if ok else "FAILED", obj["id"], "loc_req")
