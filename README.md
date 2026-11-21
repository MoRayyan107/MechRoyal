# MechRoyal: survivor bot

A bot for the Mech Royale track of the Builders Buildathon (protocol `mechfight/1`).
Score is placement, so the bot is built survival-first. Standard-library Python 3.13, no dependencies.

| File | What it is |
| --- | --- |
| `Dockerfile` | The submitted image (pinned `python:3.13.9-slim-bookworm` by digest). |
| `bot.py` | Decision logic: zone, stuck recovery, defence, weapon choice, upgrades. |
| `loot.py` | Safe weapon pickup (laser / lightning within 60 m when nobody is around). |
| `nav.py` | Map grid (4 m cells) from `/arena`, time-sliced walking-distance field, line of sight. |
| `mechfight.py` | The kit's protocol wrapper, unchanged. |
| `tools/stress.py` | Offline tests: replay timing, fuzzed observations (incl. pickups and held weapons), a crude route walker. |
| `NOTES.md` | Design log and every submitted commit with its score. |
| `protocol.md`, `mechfight.schema.json`, `check/`, `arena/`, `python/`, `ts/`, `KIT_README.md` | The organisers' starter kit, kept for reference and testing. |

## Test before every submission

```sh
python tools/stress.py
python check/check.py -- python -u bot.py
docker build --platform linux/amd64 -t survivor .
python check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" survivor
```
(On Git Bash for Windows, prefix the last line with `MSYS_NO_PATHCONV=1`.)
