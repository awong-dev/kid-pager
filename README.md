# Low-powered Kid Pager

## What is this and why does it exist?
Using expensive smart watches with also expensive cellular plans just so kids
can message their parents is ridiculous. The idea is to get something that
can run for a few dollars a month, be rugged in a backpack, and not require
constant changing.

Enter the Kid Pager.

This project builds a simple, customizable, location enabled, pager-style
device and accompanying Relay Server that can be fitted with an
ultra-cheapo SIM card to provide a mobile communication and location
system for coordinating with a child.

The Relay Server is designed to run on free-tier cloud services or on a
home server.  The complexity of the server is a bit above running a
Plex server.

The hardware is designed to be buildable by DIY hobbyists who know how
to do basic soldering.

Using the current bill of materials and the provided Relay Server, the
current prototype fits inside an Altoids Tin and has the following basic
features:
   * $2-$3/month for a soracom global coverage sim that talks to custom
     Chat server.
   * 2 week battery life per charge for basic usage
   * 2.9" e-ink screen so there NO blue light
   * _sluggish_ UI response (think kindle) to increase threshold for usage
   * GPS/GNSS and cell-tower location
   * full QWERTY keyboard
   * Temperature monitor cause everyone needs hour read of their kid's
     backpack temperature.

Because it is text only, the data usage is tiny so the data-only Soracom
sim never goes much above the base rate of $2.

We've been using this our family for about 2 months now with the thing being
beaten around inside a kid's backpack.

## Basic functionality and how it works

### Kidpager Relay Server + Native Chat
The system relies on a web-technology Relay Server for handling chat messages,
keeping location data, doing device management, pushing firmware updates, etc.
The Relay Server is the centrol point of control with all messages passing
through it so the parent can allow/deny/audit communications as makes
sense for their family.

It exports a web interface that can be used for sending/receiving chats
as well as for device management. This means your child can log into the
webpage at the home computer if they want instead of using the device
hardware allowing for access to a full keyboard when at home.

For the parent, the webpage is a "Progressive WebApp" and can be "installed"
on to your phone so that you can receive push notifications for new messages
from the native chat system.  This is actually not required for usage if
SMS or bridges into other chat systems are used (see later sections).

The Relay Server is is designed to run on the free tier of Google Firebase.
There is also a docker version so you can have complete control of the data.

### Talking to other families (Federation)

There is also federation support so that two families with different Relay
servers can register one-another to send messages back and forth.

It is also possible to host multiple families on one Relay Server. Which
one makes sense depends on data privacy considerations as to who may
access location + chat history for each family. In a multi-tenancy setup,
the admin has access to multiple families worth of data, but one admin
could host the setup for many people.

In a federated approach, only the authorized chat messages that cross the
boundries are forwarded but each family would have to run their own server.

### Extension #1: SMS
As a child gains more independence, being able to send messages to
others with SMS allows them the independence to extend their messaging
network.

Also, in emergency situations, it is frustrating to have a cellular
device, but not be able to reach someone even if you have their phone number.

Enabling SMS can be done by replacing the data-only Soracom sim with one
that supports SMS as well.

The SMS capabable SIM can be used two way:
  1. Installed directly on the device in place of the Soracom Sim.
  2. Installed in an android phone that you remote control (see Android Bridge section)

For (1), the SIM will also need a small data plan. For (2), it can be
an SMS only plan.

In either configuration, because it is a child device, all SMS
communication is controlled the Relay Server. Even in configuraiton
#1, though the device technically receives the SMS directly, the firmware
actually just passes it back up to the Relay Server so policies can be
applied. Only if it is deemed okay is it passed back to the child. This way
no random SPAM SMS messages can come through and the Relay Server always
maintains control over what the device can do.

### Extension #2: Android Bridge for SMS/WhatsApp/Google Chat/etc
This is a total hack that will likely be flaky but has worked
pretty well so far.

For this, you:
  1. Get an old android phone and leave it permanently plugged in
  at home.
  2. Install a SMS-only SIM card into that phone. (Now you have
  two SIM cards -- the $2/month Soracom in the Kid Pager device and
  a cheapo $5/month SMS-only one in the Android)
  3. Connect the phone to your wifi network to remove the need for data.
  4. Install whatever chat-apps you want and log them in.
  5. Side-load the custom Android Bridge app here that allows the Chat
  Server to remote control the Android phone.

The Android Bridge app works effectively as a remote-control for
the phone. When the pager wants to do an SMS or talk to WhatsApp
or Google chat, it sends a message to the Relay Server which then
directs the phone to forward it onwards. Incoming messages follow
that path in reverse.

Adding a new app can be done by direct Claude to vibe code an
interface.

Note that bridge does NOT give the pager device access to an entire
chat app (eg all WhatsApp contacts). Rather it subscribes to individual
conversations Relay Server. This is clunky but in a way better because
you have more fine grained control.

Why go through this complexity? Because ultimately, a custom closed
Relay Server, even with SMS, is too restrictive for the modern
communication envrionment.

However, any such bridge is fragile relying on hacks to watch
notifications, drive the UI via accessibility featuers, etc. It will likely
have bugs as time goes on.


## Hardware
The provided design uses an Altoids tin cause it looked cool. It's actually
not that great since it being metal which requires a much more expensive
external antenna that might be broken.

### Making it smaller.
The full QWERTY keyboard largest problem with shrinking the size
of the device.  After that, it's the antenna.  The battery can be
shrunk to whatever you want with a tradeoff of time-between charges.
(With a 2500 mAh battery, the system lasts about 2 weeks a charge)

So the first thing to play with is using a T-9 input system, a plastic
case so you can use a flex antenna on the inside of the case, and then
a smaller screen. You could probaby get close to an old Nokia style
candybar phone.

This would also have the benefit of possibly shaving up to $20/device off
the hardware cost as the external antennas are that much more expensive
that the internal ones.


## Privacy and Parental controls.

### Who can contact your kid + see their location
The Relay Server was designed with permission model close to a network firewall.

For each contact you can decide if

  1. Pager can send to contact
  2. Pager can receive from contact
  3. Contact may view or request location of Pager

For new and unknown contacts you can also set a policy to default allow or deny
any of the above.

A sensible configuraiton, for example, could be:
  1. Allow all send/receive from entries in a parent controlled Address Book
  2. For unknown contacts on SMS, allow if pager initiates, but log and notify the parent. This allows a kid to help a friend talk to the friend's parent.
  3. Disallow all inbound SMS to the pager number from an unknown contact, but log and notify parent. Prevents spam and also some protection against cyberbullying/harassment.

Of you could lock it down so that no one can communicate at all without the parent creating an explicit allow list,


## FAQ

### What happens with pictures and videos?

This thing is text only. The chat server will send a note that an [image] pr [video] was sent but that's it.


### What about other langauges?
The pager is built around unicode and is i18n ready! I even asked Claude to leave some hooks for an
IME (which could also be used for T-9 and predictive typing).  The problem with languages is fonts
and how much space to use in the font.  Currently, I have it doing a large chunk of Chinese and some
emojis. But you can swap that out.  The font is stored in a different partition from the code
and can be updated from the web.


### How do you update the firmware?
The Relay Server is able to push updates down for an Over The Air update (OTA) using the ESP32's OTA
infrastructure.  This allows pushing fonts also.  Firmware pushes use delta encoding making them
often around 50kb to 70kb in size which works prefectly fine in a 100Mb/month data plan.


### How does the protocol work?

In a nutshell, a provisioend Pager device maintains a connection to an MQTT
broker which provides a bidirectional channel to and from the Relay Server's
REST APIs. Provisining bind a pager to a broker and provides certs to
authenticate, sign, and/or encrypt messages as needed.

This channel is used to sent chat messages, location, and status updates. Data
is encoded in signed CBOR blob.

MQTT is used instead a direct TCP connection because it allows the Pager moden
to make heavy use of of eDRX power saving mode on LTE-M where it can sleep
while still staying on the network.

On a non-Soracomm sim, this connection is done with MQTTS using the provisioned
certs. This ensures an encrypted transmission across the open internet back to
the Relay Server, but incurs a fairly high TLS overhead for sending really short
messages.

With a Soracom SIM, Soracom Beam is used to avoid this. Instead of doing TLS
ourselves, plain MQTT is used to the Soracom Beam server. For this first hop,
we rely on the LTE layer to provide encryption which vastly reduces data usage
from the Pager on the cell plan. Soracom's Beam's proxy then creates a MQTTS
connection to our broker for transiting the open internet. Soracom's security
model guarnatees a secure network between the LTE termination and their
Beam endpoint so the threat model basically reduces to nation state that has
broken into a teleco system. That point which seems a good trade for saving
a lot of encryption handshake overhead (data usage + battery usage).

The Relay Server itself is just standard web techs (https, etc).


### How does location tracking work?

Location is derived from both cell-tower triangulation as well taking an
optional GNSS (superset of GPS) "fix" on the current location and broadcast
to the server.  The frequency of updates depends on whether the device is
in motion.  The device has an accelerometer which, when activated, causes
a faster location send. Otherwise, if it's just sitting in a backpack
for an hour, energy isn't wasted on updating location.

By nature of being on a cellular network, this is always some location
data avaliable for free just by nature of talking to a cellular antenna.
Within this part of Seattle, the precisions is frequently as good as
GNSS while also working inside buildings (that have cell signal) and
also being many times more power efficient.

However, if wanted, a GNSS antenna can be fitted on the device so it
can do a real GPS fix when there is line-of-sight to the sky.


### Can location be requested?

Yup. The Relay Server can send a signal down to tell the firmware that
location has been requested.


### Can I introduce filters on messages?

This thing is fairly simple web app that was almost entirely built with Claude
so by default there's no such functionality.

But because it is so simple, if you wanted to modify it so that messages
pass through some filter to do someething (eg, search for the word "applejack"
so that you can send an emergency broadcast to the City Spies), it'd be
very easy.

Or if you want to get super fancy, you could imagine sending everything into
an LLM that tries to do some sort of advanced cyberbullying detection...or
even process a code-switched multi-lingual chat to always provide some
sort of base translation.

Honestly, the sky's the limit.

But, personally, I think the expected use case is meant for "r u here yet?"
messages where the response is stuff like "almost. 5m."


## Technical details

### Repository layout

| Path | What it is |
|---|---|
| `docs/` | Design documents. Start at `docs/README.md`. `PROTOCOL.md` is the authoritative wire contract that code conforms to. |
| `firmware/` | ESP-IDF firmware for the Walter (ESP32-S3 + Sequans GM02SP) device. See `firmware/README.md` for build and bench steps. |
| `relay/` | FastAPI relay: webhook ingest, routing, delivery backends, admin and conversation APIs, device provisioning, authentication, retention. See `relay/README.md` to run it locally. |
| `web/` | Next.js + MUI web app on Firebase (Auth, Firestore listeners, FCM). See `web/README.md`. |
| `bridge-android/` | Android app that relays a member's SMS, voice and chat conversations to the relay (the bridge phone). |
| `home/` | Docker Compose stack for running the whole service on one machine you own. `setup.sh` writes its configuration. See `home/README.md`. |
| `hardware/` | Carrier board and enclosure design files. |
| `infra/` | Terraform for GCP (Cloud Run, Firestore, Firebase Hosting and Auth, Scheduler, Tasks, Secret Manager, Workload Identity Federation). See `infra/README.md`. |
| `tools/` | `pager_client.py` (simulated device and server driver), `e2e_v2.py` (end-to-end suite against the local stack), `send.py`, `provision.py`, `emqx_setup.py`. |
| `.github/workflows/` | `ci.yml` runs the relay unit tests and the end-to-end suite on push. `deploy.yml` builds the relay image and applies Terraform on a push to `main`. |

## Running your own server

The whole service runs on one machine you own, such as a Raspberry Pi, a NAS or a mini PC, with
Docker. You need a name that points at your home (a dynamic-DNS name is fine), a router that can
forward ports to that machine, and a reverse proxy (nginx or traefik) that already serves https
for that name.

```bash
cd home
./setup.sh               # asks for your server's name, writes the configuration, prints what to add to your proxy
docker compose up -d     # builds and starts the relay and the broker
```

`setup.sh` prints the nginx or traefik configuration, the ports to forward on your router and the
address to open to create the first admin. [`home/README.md`](home/README.md) is the full runbook:
ports, certificates, running without a proxy of your own, backups, automatic security updates and
Soracom Beam.

## Running a development stack

This is for working on the code, not for real use. Run the local stack (broker, Firebase emulators
and relay) and the end-to-end suite:

```bash
# Start the local stack (EMQX, Firebase emulators, relay)
cd relay
docker compose up -d --build
cd ..
python3 tools/emqx_setup.py
```

```bash
# [optional] Run all test scenarios, in text and CBOR wire encodings
relay/.venv/bin/python tools/e2e_v2.py
relay/.venv/bin/python tools/e2e_v2.py --wire cbor

# Or run individual scenarios
relay/.venv/bin/python tools/e2e_v2.py bootstrap setup_code address_book
```

Scenarios: `bootstrap`, `text_roundtrip`, `allowlist`, `republish`, `location_periodic`,
`location_on_demand`, `retention`, `bytes`, `address_book`, `setup_code`, `sms_log`, `bridge`,
`bridge_voice`, `bridge_whatsapp`, `receipts`, `bridge_health`. The implementation is in
`tools/e2e_v2.py`.

Unit tests and manual checklists are documented in `relay/README.md`, `web/README.md` and
`firmware/README.md`.
