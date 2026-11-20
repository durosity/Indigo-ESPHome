# ESPHome for Indigo

Control and monitor [ESPHome](https://esphome.io) boards directly from Indigo over the ESPHome native API, and
validate, compile and install their firmware through the ESPHome Device Builder, all without Home Assistant.

By Durosity.

## Features

- **Native API connection** per board, with encryption-key support, automatic reconnect with backoff and mDNS
  fast-reconnect. States are pushed live; nothing polls.
- **Every common entity type** becomes a proper Indigo device:

  | ESPHome | Indigo device | Notes |
  |---|---|---|
  | sensor | Sensor | Value rounded to the YAML `accuracy_decimals`, unit shown, icon by device class |
  | binary_sensor | Sensor (on/off) | Motion / door / window icons by device class |
  | text_sensor | Custom | `value` state |
  | switch | Relay | |
  | button | Relay | Turn On = press |
  | light | Dimmer | Brightness, RGB, white, colour temperature, effects, transitions |
  | cover / valve | Dimmer | Brightness = position; stop and tilt actions |
  | fan | Speed control | Speed level and index, oscillation, direction, presets |
  | lock | Relay (lock subtype) | Lock, unlock, open/unlatch, codes |
  | number / select / text | Custom | Writable from Indigo actions |
  | climate | Thermostat | Modes, setpoints (single and two-point), fan, presets, swing, humidity |
  | event | Custom + trigger | "ESPHome Event Received" trigger, filterable by event type |

- **API actions** (`api: actions:` in YAML) can be run from Indigo with arguments, including variable substitution.
- **Entity sync:** devices are created automatically when a board connects. Entities later removed from the firmware
  are flagged "removed" rather than deleted. You can rename Indigo devices freely.
- **Board logs** can be forwarded to the Indigo Event Log, at a level you choose per board.
- **Firmware management** via the ESPHome Device Builder: validate, compile, compile + install OTA, an
  "update available" state, an "Update All Out-of-date Boards" action and a "Firmware Build Finished" trigger.
  Full build logs are written to the plugin's log folder.

## Requirements

- Indigo 2023.2 or later (Python 3)
- ESPHome boards with `api:` enabled
- For firmware management, an **ESPHome Device Builder** (the dashboard shipped with ESPHome 2026.6 and later)
- For USB flashing, a USB data cable. Most ESP32 boards need no driver on current macOS; some CH340/CP210x boards
  do.

## Adding a board

1. Create a new device of type **ESPHome Board**.
2. Enter the board's IP address or `name.local`, and the `api: encryption: key:` from its YAML.
3. Save. The plugin connects and creates Indigo devices for the board's entities, in a folder named after the board.

## Firmware management with the Device Builder

The Device Builder does the compiling. The plugin drives it, so ESPHome's toolchains never touch Indigo's Python.
On a Mac it runs well in Docker or OrbStack:

```yaml
# docker-compose.yml
services:
  esphome:
    image: ghcr.io/esphome/esphome:stable
    container_name: esphome
    volumes:
      - ./esphome-config:/config
    ports:
      - "6052:6052"
    environment:
      - ESPHOME_USERNAME=admin
      - ESPHOME_PASSWORD=change-me
    restart: unless-stopped
```

Then, in **Plugins → ESPHome → Configure**, tick **Enable**, set the URL (e.g. `http://localhost:6052`) and enter the
username and password.

- Boards are matched to their YAML by node name. To match manually, set the file name in the board's device settings.
- **Install to → "The address Indigo is connected to"** is the right choice for Docker on macOS. Containers there
  can't see mDNS, so the plugin passes the board's IP to the Device Builder.
- Edit YAML in the Device Builder's own editor. Use **Plugins → ESPHome → Open Device Builder in Browser**.
- The first flash of a brand-new board still has to be done over USB, from the Device Builder or web.esphome.io.

## Setting up a brand-new board

**Plugins → ESPHome → Open Board Setup Page…** opens a page served by Indigo's own web server, so it uses your
Indigo login. It takes a board from nothing to an Indigo device in four steps:

1. **New board:** give it a name, pick the board type from the Device Builder catalogue, and optionally enter Wi-Fi
   details. The Device Builder writes a starter configuration with Wi-Fi, OTA and a unique API encryption key.
2. **Configure:** edit the YAML on the page, then Save (⌘S) and Validate. Output streams to the page.
3. **Flash:** plug the board into the Mac and choose **Flash over USB**. The Device Builder compiles it, and the plugin
   downloads the factory image and writes it with esptool on the Mac itself. (A Device Builder running in Docker
   can't see the Mac's USB ports, which is why the flash happens locally.) If it won't connect, hold the board's
   BOOT button while it starts. After the first flash, use **Install wirelessly**.
4. **In Indigo:** with "Add to Indigo when it finishes" ticked, the plugin creates the ESPHome Board device
   automatically, using `name.local` and the generated key. It connects once the board joins Wi-Fi, and its entities
   appear as devices.

The page is at `/message/com.durosity.indigoplugin.esphome/ui/` on your Indigo web server. Set the server's address
under *Indigo web server URL* in the plugin config if it isn't `http://localhost:8176`.

## Board setup page tools

- **Add module:** ready-made YAML for common modules (DHT22, BME280, BH1750, DS18B20, HX711 load cells, analogue,
  LD2410 mmWave, PIR, door contacts, buttons, relays, LEDs, WS2812 strips, Indigo link, diagnostics, Bluetooth proxy).
  The pin picker only offers pins that are safe on your board's chip and flags pins already in use. Modules are
  merged into your existing sections, and entity names that would clash are caught before ESPHome rejects them.
- **Editor:** line numbers and colouring. After a failed Validate, **Go to line N** jumps to the problem.
- **Changes:** what you've edited since the last install from this page.
- **History:** dated backups taken nightly and after every install. Compare any of them with the editor, or restore one.
- **Live logs:** for boards in Indigo, stream the board's log onto the page while you watch it.
- **Find boards:** finds ESPHome boards already on your network and adds them to Indigo.
- **Wi-Fi & secrets:** set the Wi-Fi details and other secrets. Values are write-only and never shown again.

## Connection health

Each board has an **offline grace period** (default 30 s). Drops shorter than that are logged and counted but don't
mark the board or its devices offline, so a Wi-Fi blip doesn't flap your sensors or fire triggers. The board device
shows *Disconnects (last 24h)*, *Disconnects (total)*, *Last Disconnect* and *Connected Since*. The
**ESPHome Board Went Offline** trigger fires only for real outages.

## Indigo link (two-way)

Tick **Share Indigo states** and/or **Allow this board to control Indigo** in the board's settings, then use
ESPHome's `homeassistant` platforms. The plugin answers in Home Assistant's place.

```yaml
api:
  homeassistant_states: true      # board reads Indigo
  homeassistant_services: true    # board acts on Indigo

sensor:
  - platform: homeassistant
    id: lamp_level
    entity_id: device.123456789   # Indigo device id (or device.bedside_lamp by name)
    attribute: brightnessLevel    # any device state; without it you get on/off
text_sensor:
  - platform: homeassistant
    id: house_mode
    entity_id: variable.house_mode

binary_sensor:
  - platform: gpio
    pin: {number: GPIO33, mode: INPUT_PULLUP, inverted: true}
    name: "Bedside Button"
    on_press:
      - homeassistant.action:
          action: indigo.run_action_group
          data: {group: "Goodnight"}
      - homeassistant.event:
          event: esphome.bedside_button   # fires the "ESPHome Board Event" trigger
```

Actions a board can call:

- `indigo.run_action_group` (`group`)
- `indigo.turn_on` / `indigo.turn_off` / `indigo.toggle` (`device`)
- `indigo.set_brightness` (`device`, `level`)
- `indigo.set_variable` (`variable`, `value`)
- `indigo.log` (`message`, `level`)

Devices, variables and action groups can be given by id or name. Values read from Indigo stay internal to the board;
they don't appear as Indigo devices.

## Bluetooth room presence

1. Add the **Bluetooth proxy** module to one ESP32 per room and install it. Set each board's **Room** in its
   Indigo settings.
2. Create a **Bluetooth Tracker** device for each thing to follow:
   - **MAC address:** for tags and beacons with a fixed address.
   - **IRK:** for iPhones, Apple Watches and modern phones, which change their address every few minutes.
   - **iBeacon UUID:** optionally with major and minor.
3. The tracker shows **Room**, **Previous Room**, **Signal**, **Heard By** and **Last Seen**. Its on/off state means
   present or away, which works in HomeKit as an occupancy-style sensor. **Room switch margin** stops it flapping
   between neighbouring rooms.

**Getting an Apple device's IRK.** The usual method is on a Mac signed in to the same Apple ID:

1. Open **Keychain Access** and search for "Bluetooth".
2. Open the entry for the iPhone or Watch and tick **Show password**. You'll need your Mac password.
3. Copy the **Remote IRK** value.

Paste it as given; base64 or hex both work, and the plugin tries both byte orders.

## Backups and overnight updates

- **Nightly backups:** turn them on in the plugin config. Each board's YAML is backed up only when it has changed.
  Copies live in Indigo's Preferences/Plugins folder for this plugin.
- **Backing up secrets:** set **Device Builder config folder** to the Mac folder mapped to `/config` and
  `secrets.yaml` is included too. The Device Builder never sends secret values over its API.
- **Overnight updates:** installs ESPHome updates on out-of-date boards during the window you choose,
  e.g. 03:00–05:00.

## Actions

- **Device actions:** Set Number / Select / Text; Press Button; light brightness with transition and light effect;
  cover stop and tilt; valve stop; fan options; lock command; climate options.
- **Board actions:** Reconnect, Sync Entities, Validate, Compile, Compile and Install OTA.
- **General actions:** Run ESPHome API Action, Check Device Builder for Updates, Update All Out-of-date Boards.
- **Triggers:** ESPHome Event Received, ESPHome Board Event, ESPHome Board Went Offline, Firmware Build Finished.

## Licence

MIT, see `LICENSE`.

The plugin downloads its Python dependencies at install time and doesn't bundle them. They keep their own licences:
aioesphomeapi (MIT), aiohttp (Apache 2.0) and esptool (GPL 2.0, used for USB flashing).
