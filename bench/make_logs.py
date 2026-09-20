import json, os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# synthetic log corpus: mostly noise, a few genuinely salient lines
random.seed(1337)
NOISE = [
    "[{ts}] DEBUG cache hit key=session:{n} ttl={n2}",
    "[{ts}] INFO  heartbeat worker={n} queue_depth={n2}",
    "[{ts}] DEBUG metric flush batch={n} duration={n2}ms",
    "[{ts}] INFO  request GET /health 200 {n2}ms",
    "[{ts}] DEBUG pool acquire conn={n} idle={n2}",
]
SALIENT = [
    "[{ts}] ERROR payment gateway timeout after 30s order={n} attempt=3/3",
    "[{ts}] FATAL disk write failed on /var/lib/pg/data: No space left on device",
    "[{ts}] ERROR unhandled exception in tax_service.calculate: Decimal.InvalidOperation",
    "[{ts}] WARN  connection pool exhausted active=200 idle=0 waiting=187",
    "[{ts}] ERROR signature verification failed for webhook id={n}",
]
def line(template, n):
    return template.format(ts=f"2026-09-19T10:{n%60:02d}:{n%60:02d}Z", n=n, n2=n % 97)
def build(total=900, salient_every=60):
    rows = []
    for i in range(total):
        if i and i % salient_every == 0:
            rows.append(line(random.choice(SALIENT), i))
        else:
            rows.append(line(random.choice(NOISE), i))
    return rows
if __name__ == "__main__":
    rows = build()
    print(json.dumps({"log": rows}, indent=1))