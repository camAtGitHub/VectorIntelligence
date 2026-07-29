# Archived: install-time chipper patches

**Status: archived / being removed.**

Do **not** add new `*.py` scripts here. Do **not** design features as
“another install patch.”

## Where chipper changes go now

Edit and build **monorepo `wire-pod/`** (sibling of `VectorIntelligence/`):

```text
wire-pod/chipper/pkg/wirepod/...
```

Ambient, sensors, face probe, behavior-tick, speech volume, conversation mode
(Save Chat only), robotsession, wake-word mute, etc. are **normal in-tree Go**.
Sensor TTS cooldowns (`SENSOR_TTS_COOLDOWN_S` / `SENSOR_TTS_GLOBAL_COOLDOWN_S`)
live only in monorepo `wire-pod/.../ttr/sensor_reactions.go`, not this archive.

## Why these files still exist

Historical install scripts (`linux/install.sh`, `windows/install.ps1`) and old
plans under `docs/superpowers/` may still reference this directory. Treat the
scripts as **reference-only** while install is migrated fully onto the monorepo
chipper tree. They may be deleted once no path depends on them.

## Agent / docs policy

See:

- repo root `agents.md`
- `VectorIntelligence/AGENTS.md` (Chipper source of truth / patches archived)
- `README.md`, `NEXT_STEPS.md`
