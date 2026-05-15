# Rove Control Interface

Python control interface that reads input from an Xbox controller or Steam
Deck, converts it into a `telemetry.RoveControl` protobuf message, and
ships it to the rover over UDP. Haptic feedback flows back to the
controller based on the active strategy.

## Architecture

Two patterns do most of the structural work:

**Template method** (`ControllerBase`) fixes the polling loop — open
device, read input, build message, send UDP, trigger rumble, sleep to
rate — and leaves four hooks (`_open_device`, `_close_device`,
`_read_input`, `_create_haptic`) for concrete controllers. `XboxController`
and `SteamDeckController` fill those in; everything else about the loop
lives in the base class.

**Strategy** (`ControlStrategy`) converts a neutral `ControllerInput`
snapshot into a populated `RoveControl` message. Because the controllers
emit the *same* `ControllerInput` regardless of hardware, every strategy
works with every device. Swapping a strategy at runtime (`controller.set_strategy(...)`)
swaps the entire control scheme without touching any other layer.

```
┌───────────────────┐    ControllerInput    ┌──────────────────┐    RoveControl     ┌──────────┐
│ XboxController    │──────────────────────▶│ ControlStrategy  │───────────────────▶│ UdpSender│──▶ rover
│ SteamDeckController│                       │ (Tank / Arcade)  │                    └──────────┘
└──────────┬────────┘                       └────────┬─────────┘
           │                                         │
           │              HapticCommand              │
           └◀────────────────────────────────────────┘
```

### Directory layout

```
capra_teleop_interface/
├── __main__.py              CLI entry: parse args, build controller + UI, run
├── ui_server.py             HTTP server: state, estop, strategy, sensor proxy, IK proxy
├── controllers/             Template-method controller loop
├── strategies/              ControllerInput → RoveControl translators
├── haptics/                 SDL2 + Steam Deck rumble backends
├── network/                 Non-blocking UDP send + telemetry receive
├── proto/core/              Generated protobuf
├── config/default.yaml      Operator defaults
├── ui/                      Blueprint.js SPA (Vite + React + TypeScript)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx          Tab shell, /state polling, send-gate sync
│   │   ├── api.ts           Typed fetch wrappers — every UI call goes through here
│   │   ├── styles.css       Layout chrome (Blueprint owns the widgets)
│   │   └── tabs/
│   │       ├── ControlTab.tsx    strategy buttons, telemetry, E-stop
│   │       ├── DataTab.tsx       live sensor cards (auto-subscribed on tab enter)
│   │       └── SettingsTab.tsx   IK collision toggle (room for more)
│   └── dist/                Built bundle (checked in; ui_server.py serves this)
└── scripts/
    └── build_ui.sh          npm install + vite build → ui/dist/
```

The UI splits into three tabs:

- **Control** — switch control mode, view sent/received state, E-stop.
  This is the only tab that flips the send-gate open. Leaving it stops
  outbound commands at the controller's send loop, so settings/sensor
  pokes can't accidentally drive the robot.
- **Data** — `/discover`'s every sensor on rove_sensor_api, subscribes to
  each one via a UDP proxy in `ui_server.py` (the browser can't speak
  UDP), and renders live values. Unsubscribes on tab leave so we're not
  holding sockets in the background.
- **Settings** — runtime toggles like collision-aware IK, proxied to
  rove_ik_engine. Add new toggles by editing `tabs/SettingsTab.tsx`.

Adding a new tab is one entry in `App.tsx`'s `TAB_DEFS` plus a component
that takes `{ active, state }`. `active=true` is the signal to start any
side-effects (polling, subscribing); on false, tear them down.

## Run

One command, fully self-bootstrapping:

```bash
./scripts/run.sh                            # uses config/default.yaml
./scripts/run.sh --device xbox              # override device
./scripts/run.sh --host 192.168.1.50 --port 5005
CONFIG=config/my_robot.yaml ./scripts/run.sh
```

`scripts/run.sh` is the only command you need. On first run it:

1. Creates a Python venv (`.venv/`) and installs `requirements.txt`.
2. Compiles protobufs (whenever any `.proto` is newer than the build).
3. Installs Node 20 LTS via [nvm](https://github.com/nvm-sh/nvm) if
   `node`/`npm` aren't already on `$PATH` — per-user, no sudo, works on
   Arch (Steam Deck), Ubuntu, Debian, Fedora.
4. Runs `npm install` + `vite build` into `ui/dist/` (only when UI
   sources are newer).
5. Downloads the SDL controller-mapping DB.
6. Kills any stale teleop process so the UI port is free.
7. `exec`s `python -m capra_teleop_interface`.

All extra flags are forwarded verbatim — see `python -m capra_teleop_interface --help`.

### UI dev loop (hot reload)

```bash
cd ui && npm install && npm run dev    # http://localhost:5173
```

The Vite dev server proxies `/api/*`, `/state`, `/estop`, `/resume`,
`/strategy` to the Python process at `:8765`, so the SPA hot-reloads
against a live teleop instance.

### Standalone scripts (rarely needed)

`scripts/run.sh` does everything, but if you want to invoke pieces
directly:

```bash
python scripts/build_protos.py     # just regenerate proto/*_pb2.py
./scripts/build_ui.sh              # just rebuild the UI
python scripts/validate_protos.py  # listen for RoveControl frames + validate
```

## Running
```bash
# Xbox controller, tank drive, to rover at 192.168.1.50:5005
python -m control_interface --host 192.168.168.44 --port 5005 \
    --device xbox --strategy tank

# Steam Deck, arcade + arm
python -m control_interface --host 192.168.168.44 --port 5005 \
    --device steamdeck --strategy arcade

# Pick a different controller by index if multiple are connected
python -m control_interface --host 192.168.168.44 --port 5005 \
    --device xbox --device-index 1
```

Press `BACK + START` together to cleanly stop the loop, or send SIGINT
(Ctrl-C).

## Adding a new strategy

Subclass `ControlStrategy`, implement `build_message`, optionally
`compute_haptics`, register it in `__main__.STRATEGIES`:

```python
class ClimbStrategy(ControlStrategy):
    name = "climb"

    def build_message(self, inp):
        msg = RoveControl_pb2.RoveControl()
        # ... your mapping here ...
        return msg
```

The controller doesn't change. Haptics don't change. Only the mapping
changes. That's the whole point of the strategy split.

## Adding a new controller

Subclass `ControllerBase` and implement the four hooks:

```python
class PS5Controller(ControllerBase):
    def _open_device(self): ...
    def _close_device(self): ...
    def _read_input(self) -> ControllerInput: ...
    def _create_haptic(self) -> HapticFeedback: ...
```

Register in `__main__.DEVICES` and every existing strategy works with it
automatically.

## Haptic feedback

Both controllers expose rumble through SDL2. On the Steam Deck, Steam
Input forwards SDL rumble calls to the real haptic motors — so the same
`Sdl2Haptic` code path works. Strategies return a `HapticCommand` from
`compute_haptics`; the base loop routes it to the active backend.
Pass `--no-haptics` to suppress rumble entirely.

## Network

`UdpSender` uses a non-blocking socket. If the OS buffer is full we drop
the frame rather than stall the control loop — at 50 Hz the next frame
is 20 ms away and stale input is worse than a dropped packet.
