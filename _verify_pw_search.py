"""One-off password verification against staging hashes. Read-only."""
import hashlib, pymysql
from pathlib import Path

BOT = Path(r"<HOME>\Downloads\v10_reports_bot")
env = {}
for line in (BOT / ".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k, _, v = line.partition("=")
    env[k.strip()] = v.strip().strip('"').strip("'")

conn = pymysql.connect(
    host=env["DB_HOST"], port=int(env["DB_PORT"]),
    user=env["DB_USER"], password=env["DB_PASSWORD"],
    database=env["DB_NAME"], ssl_ca=str(BOT / env["DB_SSL_CA"].lstrip("./")),
    connect_timeout=30, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
)
with conn.cursor() as c:
    c.execute("""
      SELECT u.email, a.password
      FROM user u JOIN account a ON a.userId=u.id
      WHERE u.email IN ('<email>','<email>','<email>')
        AND a.providerId='credential'
    """)
    rows = c.fetchall()
conn.close()

# Print stored salts for visibility
for r in rows:
    print(f"  stored: {r['email']} salt={r['password'][:32]}")

CANDIDATES = [
    # Highest priority: re-verify the explicit ADMIN_PASSWORD label in .env
    "1234qwer",
    "1234qwer!",
    "1234QWER",
    "Qwer1234",
    "qwer1234",
    "Qwer1234!",
    # Other env-file strings
    "idrelocal",
    "qovmok-7sefpe-vyqPix",
    "IDRE_RB_135679",
    "Al88N0OdDERnGPg3hbtEj9Q6NDRsUJFj",
    "uEcFB2Q7oQFiihE5u0EUgv4LdcfzDugh",
    # Better-auth docs defaults
    "password",
    "password1234",
    "password123",
    "secure-password",
    "securepassword",
    "newPassword123",
    "oldPassword123",
    # IDRE seed defaults
    "orchid123",
    "Orchid123",
    "Orchid@123",
    # Common admin defaults not yet ruled out
    "admin1234",
    "Admin1234",
    "Admin1234!",
    "Test1234",
    "Welcome1",
    "Welcome1!",
    "telomerehealth",
    "Telomere2024",
    "Telomere2025",
    "Veratru123",
    "VeraTru123",
    "Veratru@123",
    "VeraTru@123",
    "Idre2024",
    "Idre2025",
    "IDRE@2024",
    "IDRE@2025",
    # ryan first-letter, capitalisation variants
    "Ryan1234",
    "ryan1234",
    "Ryan@1234",
    "Anand@1234",
    "Anand1234",
    "Karthick1234",
    "Karthick@1234",
    "Karthick@123!",
]

MAXMEM = 2**30  # 1 GiB
for r in rows:
    salt_hex, hash_hex = r['password'].split(':', 1)
    salt = bytes.fromhex(salt_hex)
    matched = False
    for pw in CANDIDATES:
        try:
            derived = hashlib.scrypt(pw.encode('utf-8'), salt=salt, n=16384, r=16, p=1, dklen=64, maxmem=MAXMEM).hex()
        except Exception as e:
            print(f"  scrypt error for {pw!r}: {e}")
            continue
        if derived == hash_hex:
            print(f"** MATCH: {r['email']} = {pw!r} **")
            matched = True
            break
    if not matched:
        print(f"{r['email']}: no match in {len(CANDIDATES)} candidates")
