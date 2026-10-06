import serial, time, re, sys, glob, os
# Serial capture that flushes on every read and survives the port vanishing or being renamed.
# usage: serial_capture.py <outfile> <total_s> <cmd_at_s>:<cmd> ...
outfile = sys.argv[1]
total = float(sys.argv[2])
cmds = sorted([(float(a.split(':', 1)[0]), a.split(':', 1)[1]) for a in sys.argv[3:]])
t0 = time.time(); i = 0; s = None; gaps = 0; absent = 0.0
# 'wb', not 'ab': one capture, one file. Port drops within a run reopen the port, not the file.
f = open(outfile, 'wb', buffering=0)
ansi = re.compile(rb'\x1b\[[0-9;]*[A-Za-z]')
while time.time() - t0 < total:
    now = time.time()
    if s is None:
        ports = glob.glob('/dev/cu.usbmodem*')
        if ports:
            try:
                s = serial.Serial(ports[0], 115200, timeout=0.2)
            except Exception:
                s = None
        if s is None:
            absent += time.time() - now + 0.2
            time.sleep(0.2)
            continue
    try:
        chunk = s.read(8192)
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        s = None; gaps += 1; continue
    if chunk:
        f.write(ansi.sub(b'', chunk))
    if i < len(cmds) and time.time() - t0 >= cmds[i][0]:
        try:
            s.write((cmds[i][1] + "\r\n").encode()); i += 1
        except Exception:
            pass
s and s.close()
f.close()
sys.stderr.write(f"port lost {gaps} time(s), absent about {absent:.0f} s of {total:.0f} s; commands sent {i}/{len(cmds)}\n")
