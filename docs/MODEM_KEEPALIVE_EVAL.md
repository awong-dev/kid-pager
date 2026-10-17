# Evaluation: does the modem send MQTT keepalive pings on its own?

## Question

While the host (the ESP32) light-sleeps with no application traffic, does the modem send MQTT
PINGREQ packets by itself on an idle session, and at what keepalive values? The answer decides how
the firmware checks that an idle session is still alive, and how often it has to wake to do so.

## Method

A probe build connects the modem to a test broker over TLS with a chosen keepalive, subscribes to one
topic, and then sleeps with no application traffic (eDRX enabled, light sleep). The broker logs each
PINGREQ with its arrival time and gap, and it logs the time at which it closes an idle session. Each
run uses one keepalive value on one carrier and idles for 1.6 × keepalive + 30 s, or until the broker
drops the client. The sweep runs on a Soracom direct bearer and on an AT&T reseller SIM with the carrier's APN
and no PDP authentication. The probe sees only the direct bearer; the Beam bearer terminates at the
carrier's gateway and cannot be observed this way.

## Results

**Soracom direct.** Pings arrive at every keepalive tested, each answered by the broker within about 2 ms.

| Keepalive | Gap between PINGREQs |
|---|---|
| 60 s | about 60 s |
| 360 s | 360.7 s |
| 600 s | 600.8 s |
| 900 s | 899.4 s |
| 1200 s | 1200.6 s |
| 1800 s | 1800.8 s |

The idle flow stayed up for at least 30 minutes.

**AT&T reseller SIM.**

| Keepalive | PINGREQ seen | Notes |
|---|---|---|
| 60 s | yes, about every 60 s (seven in one run) | |
| 240 s | yes, at about 240 s | the cutoff lies between 240 and 480 s |
| 360 s | no | the broker closed the session at 1.5 × keepalive. In a repeat run the flow was already dead, with no ACK or RST from the network, before the broker's timeout |
| 480 s | no | the flow stayed up for 12 minutes; the broker closed it at 1.5 × keepalive (720 s) |

Earlier host-side measurements on the same carrier, at keepalive 480 s and 1800 s, saw no pings and
sessions that died after 10 to 13 minutes of idle time.

The AT&T results at 360 s and 480 s do not fit one simple idle timeout: the 480 s flow survived
longer than the 360 s flow. What the runs do show is that on AT&T the modem sends no pings above
240 s, and that an idle flow can die silently, with no RST and no modem URC.

## Conclusion the firmware relies on

- The modem does send pings on its own, but only at keepalive values the carrier path carries. On
  Soracom that is at least 1800 s; on AT&T it is 240 s, and not 360 s or 480 s.
- A modem ping costs the same radio wake as a host round trip. When a flow dies silently, the modem
  raises no URC, so only a host round trip detects it. The host re-SUBSCRIBE, with the SUBACK
  timeout, is therefore the single liveness mechanism. Modem pings are not relied on for it.
- Per carrier preset (`firmware/main/carrier.c`):
  - **Soracom** (Beam and direct): re-SUBSCRIBE after **840 s** of uplink silence, keepalive
    **1200 s**. Beam's ceiling for the keepalive is 1200 s, and Beam drops the session at 1.5 ×
    keepalive = 1800 s, so one missed re-SUBSCRIBE is survived. 840 s is well under the 1800 s
    measured above.
  - **AT&T reseller SIM** (automatic carrier selection, custom APN): re-SUBSCRIBE after **240 s** of
    uplink silence, keepalive **480 s**. 240 s is the longest interval proven on AT&T; the earlier
    300 s can be past the carrier's cut.

## Not verified

- The production firmware on Soracom Beam at 840 s and 1200 s, with an idle soak of an hour or more
  in which the keepalive does not increment and every re-SUBSCRIBE is acknowledged. The probe cannot
  see the Beam bearer.
- Other carriers, such as T-Mobile.
