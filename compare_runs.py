import re

def parse_file(path):
    rounds = []
    agg = {}
    with open(path, encoding="utf-16", errors="replace") as f:
        for line in f:
            m = re.search(r'ret=([+-]?\d+\.\d+)%.*sharpe=([+-]?\d+\.\d+).*maxDD=([+-]?\d+\.\d+)%.*judge=(\d+\.\d+)/100.*trades=(\d+).*win=(\d+)%.*RD=(\d+)', line)
            if m:
                rounds.append({
                    'ret': float(m.group(1)), 'sharpe': float(m.group(2)),
                    'maxdd': float(m.group(3)), 'judge': float(m.group(4)),
                    'trades': int(m.group(5)), 'win': int(m.group(6)), 'rd': int(m.group(7)),
                })
            am = re.search(r'Avg return / round\s*:\s*([+-]?\d+\.\d+)%', line)
            if am: agg['avg_ret'] = float(am.group(1))
            sm = re.search(r'Avg Sharpe.*:\s*([+-]?\d+\.\d+)', line)
            if sm: agg['avg_sharpe'] = float(sm.group(1))
            tm = re.search(r'Total trades\s*:\s*(\d+)', line)
            if tm: agg['total_trades'] = int(tm.group(1))
            rdm = re.search(r'Risk discipline OK\s*:\s*(YES|NO)', line)
            if rdm: agg['rd_ok'] = rdm.group(1)
    return rounds, agg

forex_rounds, forex_agg = parse_file("bt_forex_only.txt")
crypto_rounds, crypto_agg = parse_file("bt_with_crypto.txt")

labels = ["R1", "R2", "R3", "FIN"]
round_types = [labels[i % 4] for i in range(len(forex_rounds))]

print("=" * 98)
print(f"{'#':<4} {'Type':<4}  {'FOREX RET':>9} {'RD':>4} {'Judge':>6}   {'CRYPTO RET':>10} {'RD':>4} {'Judge':>6}  {'DELTA':>7}")
print("=" * 98)
for i, (f, c) in enumerate(zip(forex_rounds, crypto_rounds)):
    rtype = round_types[i]
    diff = c['ret'] - f['ret']
    flag = "  !RD" if c['rd'] < 100 else ""
    print(f"  {i+1:<2} {rtype:<4}  {f['ret']:>+8.2f}%  {f['rd']:>3}  {f['judge']:>5.1f}   {c['ret']:>+9.2f}%  {c['rd']:>3}  {c['judge']:>5.1f}  {diff:>+6.2f}%{flag}")

print("=" * 98)
print(f"\n  {'METRIC':<28} {'FOREX ONLY':>12}  {'WITH CRYPTO':>12}  {'DELTA':>8}")
print(f"  {'-'*60}")
print(f"  {'Avg return / round':<28} {forex_agg.get('avg_ret',0):>+11.2f}%  {crypto_agg.get('avg_ret',0):>+11.2f}%  {crypto_agg.get('avg_ret',0)-forex_agg.get('avg_ret',0):>+7.2f}%")
print(f"  {'Avg Sharpe (15m)':<28} {forex_agg.get('avg_sharpe',0):>+12.4f}  {crypto_agg.get('avg_sharpe',0):>+12.4f}  {crypto_agg.get('avg_sharpe',0)-forex_agg.get('avg_sharpe',0):>+8.4f}")
print(f"  {'Total trades':<28} {forex_agg.get('total_trades',0):>12}  {crypto_agg.get('total_trades',0):>12}  {crypto_agg.get('total_trades',0)-forex_agg.get('total_trades',0):>+8}")
print(f"  {'Risk discipline clean':<28} {forex_agg.get('rd_ok','?'):>12}  {crypto_agg.get('rd_ok','?'):>12}")

rd_f = sum(1 for r in forex_rounds  if r['rd'] < 100)
rd_c = sum(1 for r in crypto_rounds if r['rd'] < 100)
print(f"  {'Rounds with RD violation':<28} {rd_f:>12}  {rd_c:>12}  {rd_c-rd_f:>+8}")

r3_f  = [r for i,r in enumerate(forex_rounds)  if i%4==2]
r3_c  = [r for i,r in enumerate(crypto_rounds) if i%4==2]
fin_f = [r for i,r in enumerate(forex_rounds)  if i%4==3]
fin_c = [r for i,r in enumerate(crypto_rounds) if i%4==3]

if r3_f:
    af = sum(r['ret'] for r in r3_f)/len(r3_f)
    ac = sum(r['ret'] for r in r3_c)/len(r3_c) if r3_c else 0
    print(f"\n  {'R3 avg (COC flat, MTF fires)':<28} {af:>+11.2f}%  {ac:>+11.2f}%  {ac-af:>+7.2f}%")
if fin_f:
    af = sum(r['ret'] for r in fin_f)/len(fin_f)
    ac = sum(r['ret'] for r in fin_c)/len(fin_c) if fin_c else 0
    print(f"  {'Finals avg (MTF full 48h)':<28} {af:>+11.2f}%  {ac:>+11.2f}%  {ac-af:>+7.2f}%")
