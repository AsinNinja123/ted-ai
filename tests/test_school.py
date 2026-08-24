"""School planner: manual writes, exact reads, and read-only Ted wiring."""

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import school


PASS = FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


with tempfile.TemporaryDirectory() as tmp:
    school.DB_PATH = os.path.join(tmp, "memory.db")

    print("\n— classes —")
    biology = school.create_class({
        "name": "General Biology", "code": "BIO 101", "color": "#4caf80",
        "instructor": "Professor Green", "meeting_times": "MWF 9:00 AM",
    })
    writing = school.create_class({"name": "College Writing", "code": "ENG 110"})
    classes = school.list_classes()
    check("classes retain customized details", classes[0]["code"] == "ENG 110"
          and classes[1]["instructor"] == "Professor Green")
    check("class names are unique regardless of case",
          raises(lambda: school.create_class({"name": "general biology"}), ValueError))
    check("invalid colors are refused",
          raises(lambda: school.update_class(biology, {"color": "green"}), ValueError))

    print("\n— schoolwork —")
    now = datetime.now()
    today = now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat(timespec="minutes")
    later = (now + timedelta(days=3)).isoformat(timespec="minutes")
    old = (now - timedelta(days=1)).isoformat(timespec="minutes")
    lab = school.create_task({
        "class_id": biology, "title": "Cell lab report", "kind": "assignment",
        "priority": "high", "due_at": today, "estimated_minutes": 75,
        "notes": "Include the microscope sketches",
        "source_url": "https://school.example/biology/lab",
    })
    school.create_task({"class_id": writing, "title": "Essay outline", "due_at": later})
    school.create_task({"title": "Email advisor", "kind": "email", "due_at": old})
    check("today view contains today's lab", lab in [t["id"] for t in school.list_tasks("today")])
    check("upcoming view contains the essay", school.list_tasks("upcoming")[0]["title"] == "Essay outline")
    check("overdue view contains the general task",
          any(t["class_name"] is None for t in school.list_tasks("overdue")))
    check("unknown classes cannot receive work",
          raises(lambda: school.create_task({"class_id": 999, "title": "Ghost"}), ValueError))
    check("unsafe source links are refused",
          raises(lambda: school.update_task(lab, {"source_url": "javascript:alert(1)"}), ValueError))

    school.update_task(lab, {"status": "done", "title": "Cell lab report — submitted"})
    done = school.list_tasks("completed")
    check("finishing a task records it in the finished view",
          done[0]["id"] == lab and done[0]["completed_at"])
    school.update_task(lab, {"status": "todo"})
    check("reopening clears the completion timestamp",
          next(t for t in school.list_tasks() if t["id"] == lab)["completed_at"] is None)

    print("\n— Ted's exact read —")
    text = school.format_for_ted("all", "BIO 101")
    check("Ted can read class, title, deadline, estimate, notes, and source",
          all(part in text for part in ("General Biology", "Cell lab report — submitted",
                                        "estimate 75 min", "microscope sketches",
                                        "https://school.example/biology/lab")))
    check("Ted gets an honest empty result", "No matching" in school.format_for_ted("completed"))

    moved = school.delete_class(writing)
    essay = next(t for t in school.list_tasks() if t["title"] == "Essay outline")
    check("deleting a class preserves its work under General",
          moved["tasks_moved"] == 1 and essay["class_id"] is None)

    print("\n— local dashboard API —")
    from dashboard.app import app
    client = app.test_client()
    page = client.get("/school")
    snap = client.get("/api/school")
    check("School is a real dashboard route", page.status_code == 200 and b"Semester HQ" in page.data)
    check("the API returns the same classes and work", snap.status_code == 200
          and len(snap.get_json()["tasks"]) == 3)
    made = client.post("/api/school/tasks", json={"title": "Buy lab notebook", "kind": "other"})
    check("manual dashboard writes reach the shared store", made.status_code == 200
          and any(t["title"] == "Buy lab notebook" for t in school.list_tasks()))


root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(root, rel), encoding="utf-8") as handle:
        return handle.read()


print("\n— read-only wiring —")
from core.tools import TOOL_SCHEMAS
names = {item["function"]["name"] for item in TOOL_SCHEMAS}
check("Ted has one school read tool", "school_read" in names)
check("Ted has no school mutation tools",
      not any(name.startswith("school_") and name != "school_read" for name in names))
from core import tool_handlers
check("reading school is not classified as an action", "school_read" not in tool_handlers.ACTION_TOOLS)
check("the dispatcher calls only the exact read formatter",
      'name == "school_read"' in read("core/app.py") and "school.format_for_ted" in read("core/app.py"))
check("school language routes the read tool into Ted's menu",
      'school_read' in read("core/routing.py"))

hud = read("ui/ted_hud.html")
check("Ted's sidebar opens and closes the School surface",
      'id="schoolbtn"' in hud and "tedHud.toggleSchool()" in hud
      and "127.0.0.1:5175/school" in hud and "hideSchool:function" in hud)
school_html = read("dashboard/school.html")
check("future connections are visibly cautioned",
      "⚠" in school_html and "Not connected" in school_html and "Not built yet" in school_html)
check("the dashboard supports manual class and task CRUD",
      all(token in school_html for token in ("openClass", "removeClass", "openTask", "removeTask", "toggleDone")))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
