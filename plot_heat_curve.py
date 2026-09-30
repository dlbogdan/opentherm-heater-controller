import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

# Settings
flow_design = 60
curve_base = 29
b = 0.75
t_off = 23
t_design = -20
flow_min = 35
flow_min_on = 40
flow_max = 67

# P-offset settings
demand_neutral = 0
demand_rate = 3.8
demand_exponent = 0.35
demand_max_p_offset = 11

t_out = np.linspace(-20, 23, 200)

base_flow = []
for t in t_out:
    if t >= t_off:
        base_flow.append(curve_base)
    elif t <= t_design:
        base_flow.append(flow_design)
    else:
        demand = (t_off - t) / (t_off - t_design)
        base_flow.append(curve_base + (flow_design - curve_base) * demand**b)

base_flow = np.array(base_flow)

# P-offset envelope: max positive offset (demand=100) and max negative offset (demand=0)
def p_offset(demand_raw):
    dev = demand_raw - demand_neutral
    sign = 1 if dev >= 0 else -1
    shaped = abs(dev) ** demand_exponent
    raw_offset = sign * demand_rate * shaped
    return max(-demand_max_p_offset, min(raw_offset, demand_max_p_offset))

max_p = p_offset(100)  # max boost (high demand)
min_p = p_offset(0)    # max reduction (zero demand)

flow_with_max_p = np.clip(base_flow + max_p, flow_min, flow_max)
flow_with_min_p = np.clip(base_flow + min_p, flow_min, flow_max)
base_flow_clamped = np.clip(base_flow, flow_min, flow_max)

plt.figure(figsize=(10, 6))

# P-offset envelope
plt.fill_between(t_out, flow_with_min_p, flow_with_max_p, alpha=0.15, color='blue',
                 label=f'P-offset range ({min_p:+.1f} to {max_p:+.1f}°C)')
plt.plot(t_out, flow_with_max_p, 'b--', linewidth=1, alpha=0.5)
plt.plot(t_out, flow_with_min_p, 'b--', linewidth=1, alpha=0.5)

# Base curve
plt.plot(t_out, base_flow_clamped, 'b-', linewidth=2, label='Base flow (60/29/0.75)')

# Reference lines
plt.axhline(y=flow_min, color='r', linestyle='--', alpha=0.7, label=f'flow_min (OFF) = {flow_min}°C')
plt.axhline(y=flow_min_on, color='orange', linestyle='--', alpha=0.7, label=f'flow_min_on (ON) = {flow_min_on}°C')
plt.axhline(y=flow_max, color='purple', linestyle='--', alpha=0.7, label=f'flow_max = {flow_max}°C')
plt.axvline(x=19, color='green', linestyle=':', alpha=0.5, label='t_on = 19°C')
plt.axvline(x=23, color='red', linestyle=':', alpha=0.5, label='t_off = 23°C')

plt.xlabel('Outdoor Temperature (°C)', fontsize=12)
plt.ylabel('Flow Temperature (°C)', fontsize=12)
plt.title('Heat Curve with P-Offset Envelope', fontsize=13)
plt.legend(loc='upper right', fontsize=9)
plt.grid(True, alpha=0.3, which='both')
plt.minorticks_on()
plt.grid(True, alpha=0.15, which='minor', linestyle='-')
plt.xticks(np.arange(-20, 26, 2))
plt.yticks(np.arange(25, 76, 2))
plt.xlim(-20, 25)
plt.ylim(25, 75)
plt.tight_layout()
plt.savefig('/Users/dlbogdan/Documents/Dev/boiler-outdoor-dep/heat_curve.png', dpi=120)
print(f'P-offset range: {min_p:+.1f}°C (demand=0) to {max_p:+.1f}°C (demand=100)')
print('Saved to heat_curve.png')
