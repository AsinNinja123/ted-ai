#!/usr/bin/env python3
"""
check_neo4j.py — quick Neo4j health check for Ted.

Run it with Ted's venv so it uses the same driver + config:
    ~/ted-ai/venv/bin/python ~/ted-ai/check_neo4j.py

It tests the EXACT connection path core/memory.py uses, then reports a verdict
and a count of what's actually stored, so you can tell whether memory is broken
because Neo4j is down, the password is wrong, or it's just empty.
"""
import os, sys, socket

HOME = os.path.expanduser("~/ted-ai")
sys.path.insert(0, HOME)

URI_HOST, URI_PORT = "localhost", 7687

print("=" * 60)
print("Ted · Neo4j health check")
print("=" * 60)

# 1) Is anything listening on the bolt port?
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(2)
port_open = sock.connect_ex((URI_HOST, URI_PORT)) == 0
sock.close()
print(f"[1] Port {URI_PORT} open .......... {'YES' if port_open else 'NO  <-- Neo4j is not running'}")

if not port_open:
    print("\nVERDICT: Neo4j isn't running. Start it, then re-run this check.")
    print("  • If installed via Neo4j Desktop: open the app and press Start on your DB.")
    print("  • If installed via Homebrew:      brew services start neo4j")
    print("  • Docker:                         docker start <neo4j-container>")
    sys.exit(1)

# 2) Can we authenticate + run a query, exactly like memory.py does?
try:
    from core import memory as mem
except Exception as e:
    print(f"[2] Import core.memory ......... FAILED ({e})")
    sys.exit(1)

driver = mem._get_driver()
if driver is None:
    print("[2] Connect + auth ............. FAILED")
    print("\nVERDICT: Port is open but Ted can't connect. Almost always the password.")
    print(f"  core/memory.py is using user '{mem.USER}' and the password from")
    print("  config.NEO4J_PASSWORD (falls back to a default if not set).")
    print("  Fix: make sure NEO4J_PASSWORD in config.py matches your Neo4j password,")
    print("       or reset the DB password to match. Then re-run this check.")
    sys.exit(1)

print("[2] Connect + auth ............. OK")

# 3) What's actually stored?
try:
    with driver.session() as s:
        def count(q):
            return s.run(q).single()[0]
        people   = count("MATCH (p:Person) RETURN count(p)")
        messages = count("MATCH (m:Message) RETURN count(m)")
        replies  = count("MATCH (r:Reply) RETURN count(r)")
        facts    = count("MATCH (:Entity)-[k:KNOWS]->(:Entity) RETURN count(k)")
        goals    = count("MATCH (g:Goal) RETURN count(g)")
        habits   = count("MATCH (h:Habit) RETURN count(h)")
    print("[3] Stored data:")
    print(f"      People ........ {people}")
    print(f"      Messages ...... {messages}")
    print(f"      Replies ....... {replies}")
    print(f"      Facts ......... {facts}")
    print(f"      Goals ......... {goals}")
    print(f"      Habits ........ {habits}")

    # 4) Round-trip write/read so we know saves actually persist
    print("[4] Write/read round-trip ......", end=" ")
    mem.save_fact("TedHealthCheck", "RAN_AT", __import__("datetime").datetime.now().isoformat())
    got = mem.get_facts_about("TedHealthCheck")
    print("OK" if "RAN_AT" in got else "FAILED (write didn't read back)")

    print("\nVERDICT: Neo4j is up and Ted can read/write it.")
    if messages == 0 and facts == 0:
        print("  It's just EMPTY — memory works, there's nothing stored yet.")
        print("  Have a few real exchanges, then re-run to watch the counts grow.")
    else:
        print("  Memory is live and already holding data.")
except Exception as e:
    print(f"\nVERDICT: Connected but a query failed: {e}")
finally:
    try:
        mem.close()
    except Exception:
        pass
