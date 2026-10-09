#!/usr/bin/env bash
# End-to-end check against the local sandbox. Idempotent-ish: re-run after `docker compose down -v`.
# Steps: onboard HA → add MQTT → add Plate Gate via the setup flow API → publish a Frigate event →
# dry run shows would_open → dry run off → publish → fake garage opens → publish → skipped (cooldown).
set -euo pipefail
cd "$(dirname "$0")"
HA=http://127.0.0.1:8123
say() { printf '\n== %s\n' "$*"; }
jget() { python3 -c "import sys,json; d=json.load(sys.stdin); print(eval('d'+sys.argv[1]))" "$1"; }

say "waiting for HA"
for i in $(seq 1 120); do curl -fsS "$HA/manifest.json" >/dev/null 2>&1 && break; sleep 2; done
curl -fsS "$HA/manifest.json" >/dev/null || { echo "HA not up"; exit 1; }
sleep 5  # let integrations finish loading

TOKEN_FILE=.token
if [ ! -s "$TOKEN_FILE" ]; then
  say "onboarding a throwaway user"
  CODE=$(curl -fsS -X POST "$HA/api/onboarding/users" -H 'Content-Type: application/json' \
    -d '{"client_id":"http://127.0.0.1:8123/","name":"Sandbox","username":"sandbox","password":"sandbox-pw-123","language":"en"}' | jget "['auth_code']")
  TOKEN=$(curl -fsS -X POST "$HA/auth/token" -d "grant_type=authorization_code&code=$CODE&client_id=http://127.0.0.1:8123/" | jget "['access_token']")
  AUTH=(-H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json')
  curl -fsS -X POST "$HA/api/onboarding/core_config" "${AUTH[@]}" -d '{}' >/dev/null
  curl -fsS -X POST "$HA/api/onboarding/analytics" "${AUTH[@]}" -d '{}' >/dev/null
  curl -fsS -X POST "$HA/api/onboarding/integration" "${AUTH[@]}" -d '{"client_id":"http://127.0.0.1:8123/","redirect_uri":"http://127.0.0.1:8123/"}' >/dev/null
  echo "$TOKEN" > "$TOKEN_FILE"
fi
TOKEN=$(cat "$TOKEN_FILE"); AUTH=(-H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json')
if ! curl -fsS "$HA/api/" "${AUTH[@]}" >/dev/null 2>&1; then
  say "token expired (HA access tokens last 30 min): logging in again as the sandbox user"
  CID="http://127.0.0.1:8123/"
  FID=$(curl -fsS -X POST "$HA/auth/login_flow" -H 'Content-Type: application/json' \
    -d "{\"client_id\":\"$CID\",\"handler\":[\"homeassistant\",null],\"redirect_uri\":\"$CID\"}" | jget "['flow_id']")
  CODE=$(curl -fsS -X POST "$HA/auth/login_flow/$FID" -H 'Content-Type: application/json' \
    -d "{\"client_id\":\"$CID\",\"username\":\"sandbox\",\"password\":\"sandbox-pw-123\"}" | jget "['result']")
  TOKEN=$(curl -fsS -X POST "$HA/auth/token" -d "grant_type=authorization_code&code=$CODE&client_id=$CID" | jget "['access_token']")
  echo "$TOKEN" > "$TOKEN_FILE"; AUTH=(-H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json')
fi

flow() { # flow <handler> <json step inputs...>
  local handler=$1; shift
  local r fid
  r=$(curl -fsS -X POST "$HA/api/config/config_entries/flow" "${AUTH[@]}" -d "{\"handler\":\"$handler\"}")
  fid=$(echo "$r" | jget "['flow_id']")
  for step in "$@"; do
    r=$(curl -fsS -X POST "$HA/api/config/config_entries/flow/$fid" "${AUTH[@]}" -d "$step")
    echo "$r" | jget "['type'], d.get('step_id'), d.get('errors'), d.get('reason')" >&2
  done
  echo "$r"
}

if ! curl -fsS "$HA/api/config/config_entries/entry" "${AUTH[@]}" | grep -qE '"domain": ?"mqtt"'; then
  say "adding MQTT"
  flow mqtt '{"broker":"mosquitto","port":1883,"protocol":"5","other_settings":{"set_client_cert":false,"set_ca_cert":"off"}}' >/dev/null
fi

if ! curl -fsS "$HA/api/config/config_entries/entry" "${AUTH[@]}" | grep -qE '"domain": ?"electrifix_plate_gate"'; then
  say "adding Plate Gate through the setup screens"
  R=$(flow electrifix_plate_gate \
    '{"url":"http://frigate:5000"}' \
    '{"camera":"driveway"}' \
    '{"people_text":"Alex: XO520","near_misses":"LO120, XO540","match_distance":0,"confirmed":false}' \
    '{"people_text":"Alex: XO520","near_misses":"LO120, XO540","match_distance":0,"confirmed":true}' \
    '{"device_entity":"cover.sandbox_garage","open_on_arrival":true,"close_on_leaving":true,"cooldown_seconds":180,"require_moving":false,"auto_close_minutes":5,"zones":[]}' \
    '{}')
  echo "$R" | jget "['type'], d.get('title')"
fi
sleep 3

state() { curl -fsS "$HA/api/states/$1" "${AUTH[@]}" | jget "['state']"; }
pub() { # publish the sample event with a fresh start_time (4 s ago) so the Timing sensor is meaningful
  python3 -c "import json,time; d=json.load(open('frigate/sample_event.json')); d['after']['start_time']=time.time()-4; d['after']['frame_time']=time.time(); d['after']['id']='%.6f-sandbox' % time.time(); print(json.dumps(d))" \
    | docker compose exec -T mosquitto mosquitto_pub -t frigate/events -s; sleep 2; }

say "reset: dry run on, garage closed, cooldown 0 for the first step"
curl -fsS -X POST "$HA/api/services/switch/turn_on" "${AUTH[@]}" -d '{"entity_id":"switch.plate_gate_driveway_dry_run"}' >/dev/null
curl -fsS -X POST "$HA/api/services/cover/close_cover" "${AUTH[@]}" -d '{"entity_id":"cover.sandbox_garage"}' >/dev/null
curl -fsS -X POST "$HA/api/services/number/set_value" "${AUTH[@]}" -d '{"entity_id":"number.plate_gate_driveway_cooldown","value":0}' >/dev/null; sleep 2

say "1) dry run ON: publish XO ·520"; pub
echo "last_plate=$(state sensor.plate_gate_driveway_last_plate) last_action=$(state sensor.plate_gate_driveway_last_action) garage=$(state cover.sandbox_garage) timing=$(state sensor.plate_gate_driveway_timing)"
[ "$(state sensor.plate_gate_driveway_last_action)" = would_open ] || { echo "FAIL expected would_open"; exit 1; }

say "2) dry run OFF: publish again"; 
curl -fsS -X POST "$HA/api/services/switch/turn_off" "${AUTH[@]}" -d '{"entity_id":"switch.plate_gate_driveway_dry_run"}' >/dev/null; sleep 1
# the cover just changed?? no - but its last_changed may be recent from startup; wait out the cooldown if needed
curl -fsS -X POST "$HA/api/services/number/set_value" "${AUTH[@]}" -d '{"entity_id":"number.plate_gate_driveway_cooldown","value":0}' >/dev/null; sleep 1
pub
echo "last_action=$(state sensor.plate_gate_driveway_last_action) garage=$(state cover.sandbox_garage)"
[ "$(state cover.sandbox_garage)" = open ] || { echo "FAIL expected garage open"; exit 1; }

say "3) cooldown back to 180 s: publish again → skipped/cooldown"
curl -fsS -X POST "$HA/api/services/number/set_value" "${AUTH[@]}" -d '{"entity_id":"number.plate_gate_driveway_cooldown","value":180}' >/dev/null; sleep 1
pub
LA=$(curl -fsS "$HA/api/states/sensor.plate_gate_driveway_last_action" "${AUTH[@]}")
echo "$LA" | jget "['state'], d['attributes']['reason']"
[ "$(echo "$LA" | jget "['state']")" = skipped ] || { echo "FAIL expected skipped"; exit 1; }

say "4) test button never actuates"
curl -fsS -X POST "$HA/api/services/cover/close_cover" "${AUTH[@]}" -d '{"entity_id":"cover.sandbox_garage"}' >/dev/null; sleep 1
curl -fsS -X POST "$HA/api/services/button/press" "${AUTH[@]}" -d '{"entity_id":"button.plate_gate_driveway_test_a_plate"}' >/dev/null; sleep 2
echo "last_action=$(state sensor.plate_gate_driveway_last_action) garage=$(state cover.sandbox_garage)"
[ "$(state cover.sandbox_garage)" = closed ] || { echo "FAIL test button moved the garage"; exit 1; }

# ---------------------------------------------------------------- Phase 2: write / rollback / benchmark
ENTRY_ID=$(curl -fsS "$HA/api/config/config_entries/entry" "${AUTH[@]}" | python3 -c "import sys,json; print([e for e in json.load(sys.stdin) if e['domain']=='electrifix_plate_gate'][0]['entry_id'])")
FRIGATE=http://127.0.0.1:5005
oflow_start() { curl -fsS -X POST "$HA/api/config/config_entries/options/flow" "${AUTH[@]}" -d "{\"handler\":\"$ENTRY_ID\"}"; }
oflow_step() { curl -fsS -X POST "$HA/api/config/config_entries/options/flow/$1" "${AUTH[@]}" -d "$2"; }
oflow_get() { curl -fsS "$HA/api/config/config_entries/options/flow/$1" "${AUTH[@]}"; }
wait_progress() { # $1 flow id; poll until the flow is no longer a progress step
  for i in $(seq 1 120); do
    r=$(oflow_get "$1"); t=$(echo "$r" | jget "['type']")
    [ "$t" != "progress" ] && { echo "$r"; return 0; }
    sleep 5
  done
  echo "$r"; return 1
}
attr() { curl -fsS "$HA/api/states/$1" "${AUTH[@]}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['attributes'].get('$2'))"; }

curl -fsS -X POST "$FRIGATE/sandbox/reset" >/dev/null
say "5) write the plate-reader settings into Frigate (backup → diff → confirm → restart → verify)"
FID=$(oflow_start | jget "['flow_id']")
r=$(oflow_step "$FID" '{"next_step_id":"frigate_write"}'); echo "$r" | jget "['type'], d.get('step_id'), d.get('reason')"
echo "$r" | jget "['description_placeholders']['summary']"
r=$(oflow_step "$FID" '{"detect_native":true,"debug_save_plates":false,"confirm":true}'); echo "$r" | jget "['type'], d.get('step_id')"
r=$(wait_progress "$FID"); echo "$r" | jget "['type'], d.get('step_id')"; echo "$r" | jget "['description_placeholders']['result']"
oflow_step "$FID" '{}' >/dev/null
echo "frigate_status=$(state sensor.plate_gate_driveway_frigate_status)"
[ "$(state sensor.plate_gate_driveway_frigate_status)" = ok ] || { echo "FAIL expected frigate_status ok"; exit 1; }
curl -fsS "$FRIGATE/api/config/raw" | grep -q "known_plates" || { echo "FAIL Frigate config has no known_plates"; exit 1; }
curl -fsS "$FRIGATE/api/config/raw" | grep -q "width: 2304" && { echo "FAIL detect size should have been removed"; exit 1; }
curl -fsS "$FRIGATE/api/config/raw" | grep -q "stops at the kerb" || { echo "FAIL comment lost"; exit 1; }

say "6) a write whose Frigate never comes back → automatic rollback"
curl -fsS -X POST "$FRIGATE/sandbox/arm_failure" >/dev/null
BEFORE=$(curl -fsS "$FRIGATE/api/config/raw" | md5sum)
FID=$(oflow_start | jget "['flow_id']")
oflow_step "$FID" '{"next_step_id":"frigate_write"}' >/dev/null
oflow_step "$FID" '{"detect_native":false,"debug_save_plates":true,"confirm":true}' >/dev/null   # options changed → diff re-shown
r=$(oflow_step "$FID" '{"detect_native":false,"debug_save_plates":true,"confirm":true}'); echo "$r" | jget "['type'], d.get('step_id'), d.get('errors')"
r=$(wait_progress "$FID"); echo "$r" | jget "['description_placeholders']['result']"
oflow_step "$FID" '{}' >/dev/null
echo "frigate_status=$(state sensor.plate_gate_driveway_frigate_status)"
[ "$(state sensor.plate_gate_driveway_frigate_status)" = rolled_back ] || { echo "FAIL expected rolled_back"; exit 1; }
[ "$(curl -fsS "$FRIGATE/api/config/raw" | md5sum)" = "$BEFORE" ] || { echo "FAIL config changed despite rollback"; exit 1; }

say "7) hardware screen"
FID=$(oflow_start | jget "['flow_id']")
r=$(oflow_step "$FID" '{"next_step_id":"hardware"}'); echo "$r" | jget "['description_placeholders']['report']" | head -12
oflow_step "$FID" '{}' >/dev/null
echo "hardware sensor=$(state sensor.plate_gate_driveway_hardware)"

say "8) benchmark s-320 vs m-640, 1 minute each (about 3 minutes)"
BEFORE=$(curl -fsS "$FRIGATE/api/config/raw" | md5sum)
FID=$(oflow_start | jget "['flow_id']")
oflow_step "$FID" '{"next_step_id":"benchmark"}' >/dev/null
r=$(oflow_step "$FID" '{"candidates":["yolov9-s-320","yolov9-m-640"],"minutes":1,"ack":true}'); echo "$r" | jget "['type'], d.get('step_id')"
r=$(wait_progress "$FID"); echo "$r" | jget "['description_placeholders']['table']"
oflow_step "$FID" '{}' >/dev/null
echo "benchmark sensor=$(state sensor.plate_gate_driveway_benchmark) rows=$(attr sensor.plate_gate_driveway_benchmark rows)"
[ "$(state sensor.plate_gate_driveway_benchmark)" = done ] || { echo "FAIL benchmark not done"; exit 1; }
[ "$(curl -fsS "$FRIGATE/api/config/raw" | md5sum)" = "$BEFORE" ] || { echo "FAIL original config not restored after benchmark"; exit 1; }

say "9) one-click restore button → the config from before step 5"
curl -fsS -X POST "$HA/api/services/button/press" "${AUTH[@]}" -d '{"entity_id":"button.plate_gate_driveway_restore_frigate"}' >/dev/null
for i in $(seq 1 30); do s=$(state sensor.plate_gate_driveway_frigate_status); [ "$s" = ok ] && break; sleep 3; done
curl -fsS "$FRIGATE/api/config/raw" | grep -q "known_plates" && { echo "FAIL restore did not put the original back"; exit 1; }
echo "restored; frigate_status=$(state sensor.plate_gate_driveway_frigate_status)"

say "10) companion is online and reports the machine"
for i in $(seq 1 20); do s=$(state sensor.plate_gate_driveway_companion); [ "$s" = online ] && break; sleep 2; done
echo "companion sensor=$(state sensor.plate_gate_driveway_companion) cpu=$(attr sensor.plate_gate_driveway_companion hardware | python3 -c "import sys,ast; print(ast.literal_eval(sys.stdin.read())['cpu_model'])")"
[ "$(state sensor.plate_gate_driveway_companion)" = online ] || { echo "FAIL companion not online"; exit 1; }

say "11) install yolov9-s-320 through the Companion (from the sandbox's own model server)"
docker compose exec -T companion rm -f /frigate_config/model_cache/plate_gate/yolov9-s-320.onnx
FID=$(oflow_start | jget "['flow_id']")
r=$(oflow_step "$FID" '{"next_step_id":"install_model"}'); echo "$r" | jget "['type'], d.get('step_id'), d.get('reason')"
r=$(oflow_step "$FID" '{"model":"yolov9-s-320","source":"http://frigate:5000/models"}'); echo "$r" | jget "['type'], d.get('step_id')"
r=$(wait_progress "$FID"); echo "$r" | jget "['description_placeholders']['result']"
oflow_step "$FID" '{}' >/dev/null
docker compose exec -T companion sh -c "sha256sum /frigate_config/model_cache/plate_gate/yolov9-s-320.onnx | cut -d' ' -f1" > /tmp/pg_sha.txt
grep -q "$(cat /tmp/pg_sha.txt)" ../models-dist/SHA256SUMS || { echo "FAIL installed file checksum mismatch"; exit 1; }
echo "installed, checksum matches SHA256SUMS"

say "12) benchmark and hardware screens know what is installed"
FID=$(oflow_start | jget "['flow_id']")
r=$(oflow_step "$FID" '{"next_step_id":"benchmark"}')
echo "$r" | python3 -c "import sys,json; d=json.load(sys.stdin); opts=[f for f in d['data_schema'] if f['name']=='candidates'][0]['selector']['select']['options']; print([o['label'] for o in opts if 's-320' in o['value'] or 'm-640' in o['value']])" | tee /tmp/pg_labels.txt
grep -q "yolov9-s-320.*(installed)" /tmp/pg_labels.txt || { echo "FAIL s-320 not shown as installed"; exit 1; }
FID2=$(oflow_start | jget "['flow_id']")
r=$(oflow_step "$FID2" '{"next_step_id":"hardware"}'); echo "$r" | jget "['description_placeholders']['report']" | grep -E "Companion|stream comes from|already on the Frigate|Install model" | head -5

say "13) accuracy check on the sandbox's sample photos with two installed models"
docker compose exec -T companion rm -f /frigate_config/model_cache/plate_gate/yolov9-t-320.onnx
FID=$(oflow_start | jget "['flow_id']")
oflow_step "$FID" '{"next_step_id":"install_model"}' >/dev/null
oflow_step "$FID" '{"model":"yolov9-t-320","source":"http://frigate:5000/models"}' >/dev/null
r=$(wait_progress "$FID"); echo "$r" | jget "['description_placeholders']['result']"; oflow_step "$FID" '{}' >/dev/null
FID=$(oflow_start | jget "['flow_id']")
r=$(oflow_step "$FID" '{"next_step_id":"accuracy"}'); echo "$r" | jget "['type'], d.get('step_id'), d.get('reason')"
r=$(oflow_step "$FID" '{"models":["yolov9-t-320.onnx","yolov9-s-320.onnx"],"max_images":20,"ack":true}'); echo "$r" | jget "['type'], d.get('step_id')"
r=$(wait_progress "$FID"); echo "$r" | jget "['description_placeholders']['table']"
oflow_step "$FID" '{}' >/dev/null
echo "accuracy sensor=$(state sensor.plate_gate_driveway_accuracy) images_used=$(attr sensor.plate_gate_driveway_accuracy images_used)"
[ "$(state sensor.plate_gate_driveway_accuracy)" = done ] || { echo "FAIL accuracy not done"; exit 1; }
python3 - "$(attr sensor.plate_gate_driveway_accuracy rows)" <<'PY'
import ast, sys
rows = ast.literal_eval(sys.argv[1])
assert len(rows) == 2 and all(r["ok"] for r in rows), rows
assert any(r["images_with_vehicle"] > 0 for r in rows), "no vehicle found in the sample photos by either model"
print("rows ok:", [(r["filename"], r["images_with_vehicle"], r["vehicles_total"]) for r in rows])
PY

say "ALL SANDBOX CHECKS PASSED"
