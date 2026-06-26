import re

def parse_file(path):
    rounds = []
    with open(path, encoding="utf-16", errors="replace") as f:
        for line in f:
            m = re.search(r'ret=([+-]?\d+\.\d+)%.*sharpe=([+-]?\d+\.\d+).*maxDD=([+-]?\d+\.\d+)%.*judge=(\d+\.\d+)/100.*trades=(\d+).*win=(\d+)%.*RD=(\d+)', line)
            if m:
                rounds.append({
                    'ret': float(m.group(1)), 'sharpe': float(m.group(2)),
                    'maxdd': float(m.group(3)), 'judge': float(m.group(4)),
                    'trades': int(m.group(5)), 'win': int(m.group(6)), 'rd': int(m.group(7))
                })
    return rounds

f = parse_file("bt_forex_only.txt")
c = parse_file("bt_with_crypto.txt")

def avg(lst): return sum(lst)/len(lst) if lst else 0

labels = ["R1","R2","R3","FIN"]
rt = [labels[i%4] for i in range(len(f))]

# Group by round type
for rtype in ["R1","R2","R3","FIN"]:
    idxs = [i for i,l in enumerate(rt) if l==rtype]
    fr = [f[i] for i in idxs if i<len(f)]
    cr = [c[i] for i in idxs if i<len(c)]
    print(f"{rtype}: forex_avg_ret={avg([r['ret'] for r in fr]):+.2f}%  "
          f"crypto_avg_ret={avg([r['ret'] for r in cr]):+.2f}%  "
          f"delta={avg([r['ret'] for r in cr])-avg([r['ret'] for r in fr]):+.2f}%  "
          f"forex_RD_ok={sum(1 for r in fr if r['rd']==100)}/{len(fr)}  "
          f"crypto_RD_ok={sum(1 for r in cr if r['rd']==100)}/{len(cr)}  "
          f"forex_avg_judge={avg([r['judge'] for r in fr]):.1f}  "
          f"crypto_avg_judge={avg([r['judge'] for r in cr]):.1f}")

print()
print(f"ALL:  forex_avg_ret={avg([r['ret'] for r in f]):+.2f}%  crypto_avg_ret={avg([r['ret'] for r in c]):+.2f}%  delta={avg([r['ret'] for r in c])-avg([r['ret'] for r in f]):+.2f}%")
print(f"      forex_avg_sharpe={avg([r['sharpe'] for r in f]):+.4f}  crypto_avg_sharpe={avg([r['sharpe'] for r in c]):+.4f}")
print(f"      forex_avg_judge={avg([r['judge'] for r in f]):.1f}  crypto_avg_judge={avg([r['judge'] for r in c]):.1f}")
print(f"      forex_RD_violations={sum(1 for r in f if r['rd']<100)}  crypto_RD_violations={sum(1 for r in c if r['rd']<100)}")
print(f"      forex_total_trades={sum(r['trades'] for r in f)}  crypto_total_trades={sum(r['trades'] for r in c)}")
