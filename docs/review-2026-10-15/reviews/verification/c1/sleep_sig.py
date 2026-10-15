import os, signal, sys, time
sys.path.insert(0, "/home/user/alpaca-trading")
from agent.runtime_control import _aligned_cycle_delay
flag = {"stop": False}
def handler(signum, frame):
    flag["stop"] = True          # like main.py _stop -> request_shutdown: sets a flag, no raise
signal.signal(signal.SIGTERM, handler)
if os.fork() == 0:
    time.sleep(1.0); os.kill(os.getppid(), signal.SIGTERM); os._exit(0)
t0 = time.time()
time.sleep(6.0)                   # stands in for time.sleep(_aligned_cycle_delay(now, 60))
print(f"stop flag={flag['stop']} after SIGTERM at ~1.0 s; sleep returned after {time.time()-t0:.2f} s")
print("aligned delay examples (interval 60):",
      [round(_aligned_cycle_delay(t, 60), 3) for t in (1_000_000_020.5, 1_000_000_059.9, 1_000_000_020.0)])
