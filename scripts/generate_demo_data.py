#!/usr/bin/env python
"""Generate SYNTHETIC demo CSVs for the shopfloor-cmms demo instance.

Everything here is fictional. No real company, person, equipment or part is
represented. The output is written in EXACTLY the column format / encoding that
the existing loaders expect (they are the contract):

    file                     encoding    loader / CLI
    ----------------------   ---------   ------------------------------------
    assets.csv               utf-8-sig   cmms load-assets
    tasks.csv                utf-8-sig   cmms load-tasks
    scheduled_activity.csv   utf-8-sig   cmms load-pm-schedules
    inventory.csv            cp1252      cmms load-inventory      (20 columns)
    contacts.csv             latin-1     cmms load-contacts
    work_orders.csv          latin-1     cmms load-work-orders
    task_steps_parts.csv     cp1252      cmms load-task-steps
    part_issues.csv          cp1252      cmms load-part-issues
    efc_codes.csv            utf-8       cmms load-efc-codes

Deterministic: the RNG is seeded with a fixed constant, so the same CSVs (and
therefore the same demo database) come out every time.

Stable ids the seeding script relies on:
    assets      EID-10001 .. EID-10060   (EID-10058..10060 are retired)
    inventory   EC000001 .. EC000120 / ES-prefixed variants (see ITEM_CODES)
    work orders 20001 .. 20300

Usage:
    python scripts/generate_demo_data.py [--out data/demo]
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 20260714
TODAY = date(2026, 7, 14)  # fixed "today" so the demo is reproducible

# ---------------------------------------------------------------- vocabulary

# Equipment families -> (asset_type, department, typical line)
FAMILIES: list[tuple[str, str, str, str]] = [
    # family, asset_type, department, line
    ("Aligner", "Production", "EQ", "1K"),
    ("Curer", "Production", "EQ", "1K"),
    ("Prober", "Metrology", "QA", "10K"),
    ("Dispenser", "Production", "EQ", "10K"),
    ("FlexBonder", "Production", "EQ", "1K"),
    ("Cleaner", "Production", "PE", "Wet Loop"),
    ("Oven", "Facility", "EQ", "Wet Loop"),
    ("Inspector", "Metrology", "QA", "10K"),
    ("Sorter", "Production", "PE", "10K"),
    ("Laser", "Production", "EQ", "1K"),
    ("Rinser", "Production", "PE", "Wet Loop"),
    ("Tester", "Metrology", "QA", "EOL"),
]

# Neutral, obviously fake people.
ENGINEERS = [
    "Jordan Lee",
    "Sam Wu",
    "Alice Fang",
    "Ben Yeh",
    "Cara Lo",
    "Lin Hsu",
    "Iris Chiu",
    "Sam Wu",
]
# Deliberately unrelated, obviously-invented names — no connection to any real roster.
CONTACT_PEOPLE = [
    ("Jordan", "Lee"), ("Sam", "Wu"), ("Alice", "Fang"), ("Ben", "Yeh"),
    ("Cara", "Lo"), ("Lin", "Hsu"), ("Iris", "Chiu"), ("Dana", "Reyes"),
    ("Nina", "Sorensen"), ("Peter", "Vogel"), ("Omar", "Haddad"), ("Elin", "Backer"),
    ("Grace", "Okafor"), ("Henry", "Baradi"), ("Ines", "Moreau"), ("Karl", "Novak"),
    ("Mona", "Duarte"), ("Owen", "Reilly"), ("Rita", "Kovac"), ("Silas", "Green"),
    ("Tara", "Blum"), ("Umar", "Diaz"), ("Vera", "Lam"), ("Wes", "Barton"),
    ("Xenia", "Roth"), ("Yuki", "Ono"), ("Zane", "Ford"), ("Aria", "Nunez"),
    ("Bruno", "Katz"), ("Celia", "Marsh"),
]

# Contract manufacturers. NOTE: the legacy `assignto` format is `VENDOR (Person)`
# and the loader's regex only accepts an ALPHANUMERIC vendor token — so the
# vendor codes carried inside work orders / PM rows are CMA and CMB, while the
# organisations in the contact book are named CMA / CMB.
VENDORS = ["CMA", "CMB"]

# Supplier organisations (inventory.supplier must equal organization.name for
# `cmms link-suppliers` to bind them).
SUPPLIERS = ["SUPA", "SUPB"]

OTHER_COMPANIES = [
    "Northwind Automation", "Bluepeak Instruments", "Crestline Robotics",
    "Delta Arc Systems", "Everline Optics", "Foxglove Fluidics",
    "Granite Bay Tooling", "Harborlight Sensors", "Ironvale Bearings",
    "Juniper Vacuum", "Kestrel Pneumatics", "Lakeshore Ceramics",
    "Meridian Filters", "Novacore Drives", "Orchard Belts", "Pinecliff Optics",
]

# Warehouse bins that exist in the storage_bin controlled vocabulary (migration 0028).
BINS = [
    "01A", "02B", "03A", "03C", "04B", "05A", "06C", "07A", "08B", "09C",
    "10A", "11B", "12D", "13A", "14C", "15A", "16B", "17C", "18A", "19B",
    "20A", "Drawer", "Staging", "Returns",
]

PART_NOUNS = [
    "O-Ring", "Vacuum Pad", "Drive Belt", "Linear Rail", "Ball Screw",
    "Proximity Sensor", "Solenoid Valve", "Air Filter", "Fuse 5A", "Relay 24V",
    "Thermocouple", "Heater Cartridge", "Dispense Nozzle", "Syringe Barrel",
    "Camera Lens", "LED Ring Light", "Timing Pulley", "Shock Absorber",
    "Pressure Regulator", "Coupling", "Servo Motor", "Encoder Disc",
    "PTFE Tubing", "Quartz Window", "Stage Chuck Seal", "Cooling Fan",
    "Power Supply 24V", "Ribbon Cable", "Gripper Finger", "Bearing Block",
]

FAULTS = [
    "will not home on X axis",
    "vacuum error at load port",
    "temperature out of range",
    "recipe aborts at step 3",
    "air pressure low alarm",
    "door interlock keeps tripping",
    "belt slipping, position drift",
    "nozzle clogged, no dispense",
    "camera focus drifts after warm-up",
    "E-stop triggered without cause",
    "coolant leak under the stage",
    "servo overload fault",
    "communication timeout with controller",
    "excessive particle count after clean",
]

ACTIONS = [
    "Replaced worn part and re-homed the axis.",
    "Cleaned sensor optics; alarm cleared after re-test.",
    "Re-seated connector; verified 3 dry cycles.",
    "Adjusted regulator to spec and logged the reading.",
    "Swapped the module with a spare; unit returned to service.",
    "Tightened coupling and re-calibrated the stage.",
    "Flushed the line and replaced the nozzle.",
    "Firmware reloaded; verified with a production lot.",
]

PM_TASKS = [
    "Monthly preventive maintenance",
    "Quarterly calibration check",
    "Semi-annual overhaul",
    "Weekly cleaning and inspection",
    "Filter replacement",
    "Lubrication service",
    "Belt tension check",
    "Vacuum system check",
    "Safety interlock verification",
    "Annual electrical inspection",
]

STEP_VERBS = [
    "Power down the tool and apply lockout/tagout.",
    "Open the maintenance panel.",
    "Inspect the drive belt for wear and cracks.",
    "Check air pressure at the regulator (target 0.5 MPa).",
    "Clean the optics with lint-free wipes.",
    "Lubricate the linear rails.",
    "Replace the inline air filter.",
    "Verify the emergency stop circuit.",
    "Run three dry cycles and record the result.",
    "Record readings in the maintenance log and close out.",
    "Check the coolant level and top up if needed.",
    "Inspect the vacuum pads for deformation.",
]

# efc = equipment failure codes (controlled vocabulary; the constant columns are
# a drift guard enforced by the loader and must match exactly).
EFC_CONSTANTS = {
    "pdd_class": "dimEquipmentFailureCode",
    "source_table": "mes.EquipmentFailureEvent",
    "source_column": "FailureCode",
    "axis": "equipment",
}
EFC_CODES: list[tuple[str, str, str]] = [
    ("efcAlign_HomeTimeout", "Aligner failed to home within timeout", "ALIGN"),
    ("efcAlign_VacuumLow", "Aligner chuck vacuum below threshold", "ALIGN"),
    ("efcCure_TempOutOfRange", "Curer temperature outside process window", "CURE"),
    ("efcCure_LampFailure", "Curer UV lamp failed to strike", "CURE"),
    ("efcDisp_NozzleClog", "Dispenser nozzle clogged", "DISP"),
    ("efcDisp_PressureLow", "Dispenser air pressure low", "DISP"),
    ("efcProbe_ContactFail", "Prober contact resistance out of spec", "PROBE"),
    ("efcProbe_StageDrift", "Prober stage position drift detected", "PROBE"),
    ("efcBond_ForceOutOfSpec", "Bonder bond force outside limits", "BOND"),
    ("efcBond_HeadOverTemp", "Bonder head over temperature", "BOND"),
    ("efcClean_FlowLow", "Cleaner rinse flow below setpoint", "CLEAN"),
    ("efcClean_DoorOpen", "Cleaner service door opened during run", "CLEAN"),
    ("efcGen_EStopTriggered", "Emergency stop triggered", "TODO"),
    ("efcGen_CommTimeout", "Controller communication timeout", "TODO"),
]

# ---------------------------------------------------------------- helpers


def mmddyy(d: date) -> str:
    """eMaint legacy date format: MM/DD/YY."""
    return d.strftime("%m/%d/%y")


def write_csv(path: Path, header: list[str], rows: list[list[object]], encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        w.writerow(header)
        for row in rows:
            w.writerow(["" if v is None else v for v in row])
    print(f"  wrote {path} ({len(rows)} rows, {encoding})")


# ---------------------------------------------------------------- generators


def gen_assets(rng: random.Random) -> list[dict]:
    """60 assets, EID-10001 .. EID-10060. The last three are retired."""
    assets: list[dict] = []
    n = 60
    for i in range(n):
        family, atype, dept, line = FAMILIES[i % len(FAMILIES)]
        seq = i // len(FAMILIES) + 1
        eid = f"EID-{10001 + i:05d}"
        retired = i >= n - 3
        assets.append(
            {
                "compid": eid,
                "comp_desc": f"{family}-{seq:02d}",
                "assettype": atype,
                "assetsubtp": family.upper(),
                "department": dept,
                "line_no": line,
                "model_no": f"{family[:3].upper()}-{rng.choice(['200', '350', '400X', '700'])}",
                "serial_no": f"SN{rng.randint(100000, 999999)}",
                "available": "No" if retired else "Yes",
                # not written to CSV, used by other generators:
                "_family": family,
                "_retired": retired,
            }
        )
    return assets


def gen_tasks(rng: random.Random) -> list[dict]:
    """40 PM task definitions."""
    tasks = []
    for i in range(40):
        family = FAMILIES[i % len(FAMILIES)][0]
        kind = PM_TASKS[i % len(PM_TASKS)]
        tasks.append({"task_no": f"TSK-{i + 1:04d}", "task_desc": f"{family} — {kind}"})
    return tasks


def gen_pm_schedules(rng: random.Random, assets: list[dict], tasks: list[dict]) -> list[dict]:
    """80 PM schedules; a handful are already overdue so the 'due' list is not empty."""
    live = [a for a in assets if not a["_retired"]]
    rows = []
    for i in range(80):
        asset = live[i % len(live)]
        task = tasks[i % len(tasks)]
        interval, unit = rng.choice(
            [(1, "Months"), (3, "Months"), (6, "Months"), (2, "Weeks"), (30, "Days")]
        )
        # ~15% already overdue, the rest spread over the coming ~90 days
        offset = rng.randint(-45, -1) if i % 7 == 0 else rng.randint(0, 90)
        next_due = TODAY + timedelta(days=offset)
        last_pm = next_due - timedelta(days=rng.randint(20, 120))
        person = ENGINEERS[i % len(ENGINEERS)]
        rows.append(
            {
                "pmid": f"PM-{i + 1:05d}",
                "compid": asset["compid"],
                "task_no": task["task_no"],
                "pmfreqx": interval,
                "pmfreq": unit,
                "pmnextdate": mmddyy(next_due),
                "lastpmdate": mmddyy(last_pm),
                "lastpmno": "",
                "dayscmpl": f"{rng.choice([3, 5, 7, 14])}.00",
                "standard": f"{rng.choice([1, 2, 3, 4])}.00",
                "estlabor": f"{rng.choice([1, 2, 3])}.50",
                "assignto": f"{rng.choice(VENDORS)} ({person})",
                "suppress": "T" if i % 23 == 0 else "F",
            }
        )
    return rows


def gen_inventory(rng: random.Random) -> list[dict]:
    """120 spare-part items. Item codes: EC000001.. / ES000001.. (legacy prefixes)."""
    items = []
    for i in range(120):
        prefix = "EC" if i % 3 else "ES"
        code = f"{prefix}{i + 1:06d}"
        noun = PART_NOUNS[i % len(PART_NOUNS)]
        family = FAMILIES[i % len(FAMILIES)][0].upper()
        # a second applicable subtype for some items (multi-value column)
        subs = [family]
        if i % 5 == 0:
            subs.append(FAMILIES[(i + 3) % len(FAMILIES)][0].upper())
        on_hand = rng.randint(0, 40)
        reorder = rng.choice([2, 4, 5, 10])
        items.append(
            {
                "item": code,
                "asset_sub": ",".join(subs),
                "sf_desc": noun,
                "vpartno": f"VP-{rng.randint(1000, 9999)}-{rng.choice('ABCDEFGH')}",
                "descrip": f'{noun} for {family.capitalize()} tools, 3/8" nominal, pack of 1',
                "location": rng.choice(BINS),
                "orderpt": f"{reorder}.00",
                "onhand": f"{on_hand}.00",
                "cost": f"{rng.randint(3, 900)}.{rng.choice(['00', '50', '25'])}",
                "lead_time": rng.choice([1, 2, 3, 4, 6, 8]),
                "obsol": "T" if i % 37 == 0 else "F",
                "stock": "T",
                "supplier": rng.choice(SUPPLIERS),
                "weblink": "",
                "photo": "",
                "comment": "Synthetic demo part." if i % 11 == 0 else "",
                "alt_item": "",
                "parnt_item": "",
                "child_item": "",
                "_low": on_hand <= reorder,
            }
        )
    # A few alternative-part and kit (BOM) links, so those UI sections are populated.
    for i in range(0, 100, 17):
        items[i]["alt_item"] = items[i + 1]["item"]
    for i in range(2, 90, 21):
        items[i]["child_item"] = f"{items[i + 1]['item']},{items[i + 2]['item']}"
    return items


def gen_contacts(rng: random.Random) -> list[dict]:
    """~20 organisations / ~30 persons. CMB is seeded by the loader itself."""
    companies: list[tuple[str, str]] = [("CMA", "Employee"), ("SF", "Customer")]
    companies += [(s, "Supplier") for s in SUPPLIERS]
    companies += [(c, "Supplier") for c in OTHER_COMPANIES]  # -> 20 companies

    rows = []
    for i, (first, last) in enumerate(CONTACT_PEOPLE):
        company, category = companies[i % len(companies)]
        cid = f"{first[:2].upper()}{last[:2].upper()}{i:02d}"
        domain = "example.com"
        rows.append(
            {
                "contactid": cid,
                "company": company,
                "category": category,
                "fname": first,
                "lname": last,
                "fullname": f"{first} {last}",
                "email": f"{first.lower()}.{last.lower()}@{domain}",
                "wphone": f"+886-2-5550-{1000 + i:04d}",
                "ext": str(100 + i),
                "mobile": f"+886-9{rng.randint(10000000, 99999999)}",
                "waddress": "1 Demo Road, PLANT-1",
                "wweb": f"https://{company.lower().replace(' ', '')}.{domain}",
            }
        )

    # Demonstrate the conservative person de-duplication path: SMWU is a known
    # alias of SAMWU99 (same person, same company) — the loader merges them.
    rows.append(
        {
            "contactid": "SAMWU99", "company": "CMA", "category": "Employee",
            "fname": "Sam", "lname": "Wu", "fullname": "Sam Wu",
            "email": "sam-wu@example.com", "wphone": "+886-2-5550-2001", "ext": "201",
            "mobile": "+886-912345678", "waddress": "1 Demo Road, PLANT-1",
            "wweb": "https://cma.example.com",
        }
    )
    rows.append(
        {
            "contactid": "SMWU", "company": "CMA", "category": "Employee",
            "fname": "Sam", "lname": "Wu", "fullname": "Sam Wu",
            "email": "Sam-Wu@example.com", "wphone": "+886-2-5550-2001", "ext": "201",
            "mobile": "+886-912345678", "waddress": "1 Demo Road, PLANT-1",
            "wweb": "https://cma.example.com",
        }
    )
    return rows


def gen_work_orders(rng: random.Random, assets: list[dict]) -> list[dict]:
    """300 work orders over the last 18 months, realistic status / type mix."""
    live = [a for a in assets if not a["_retired"]]
    rows = []
    start = TODAY - timedelta(days=548)  # ~18 months
    for i in range(300):
        wo_no = 20001 + i
        asset = live[i % len(live)]
        # deterministic-but-spread open dates across the window
        opened = start + timedelta(days=int(i * 548 / 300) + rng.randint(0, 1))
        is_pm = rng.random() < 0.35
        wo_type = "PM" if is_pm else "REACTIVE"
        person = ENGINEERS[(i + (0 if is_pm else 3)) % len(ENGINEERS)]
        vendor = rng.choice(VENDORS)

        # Status mix: older work orders are settled, recent ones are still live.
        # `O` / `H` are the legacy codes the transform maps to OPEN / CLOSED; the
        # other canonical states pass straight through the STATUS_MAP.
        age_days = (TODAY - opened).days
        if age_days > 120:
            status = "CANCELLED" if rng.random() < 0.04 else "H"  # H = legacy CLOSED
        else:
            status = rng.choices(
                ["H", "O", "IN_PROGRESS", "ON_HOLD", "CANCELLED"],
                weights=[30, 28, 22, 15, 5],
            )[0]

        # Jordan Lee is the engineer persona in the demo walkthrough: give him about
        # half of the still-open work so that logging in as him shows a real queue.
        if status in ("O", "IN_PROGRESS", "ON_HOLD") and i % 2 == 0:
            person = "Jordan Lee"

        closed_date = closed_time = edituser = diag = ""
        if status in ("H", "CANCELLED"):
            closed = opened + timedelta(days=rng.randint(0, 6))
            closed_date = mmddyy(closed)
            closed_time = f"{rng.randint(10, 18):02d}:{rng.choice(['05', '20', '35', '50'])}:00"
            edituser = person.split()[0].lower()
            if status == "H":
                diag = rng.choice(ACTIONS)

        # NOTE: work_orders.csv is latin-1 (legacy encoding) -> keep this text ASCII.
        brief = (
            f"{asset['comp_desc']} - {rng.choice(PM_TASKS)}"
            if is_pm
            else f"{asset['comp_desc']} {rng.choice(FAULTS)}"
        )
        rows.append(
            {
                "wo": wo_no,
                "compid": asset["compid"],
                "wo_type": wo_type,
                "workstatus": status,
                "brief_desc": brief,
                "diag": diag,
                # legacy external reference: a few carry an MRQ ticket number
                "comments": f"MRQ-{rng.randint(1000, 9999)}" if i % 19 == 0 else "",
                "date_wo": mmddyy(opened),
                "sch_date": mmddyy(opened) if is_pm else "",
                "time": f"{rng.randint(9, 16):02d}:{rng.choice(['00', '15', '30', '45'])}:00",
                "time_cmpl": "",
                "editdate": closed_date,
                "edittime": closed_time,
                "edituser": edituser,
                "assignto": f"{vendor} ({person})",
                # a couple of mis-created rows so the loader's filter is visible
                "miscreated": "T" if i % 97 == 0 and i > 0 else "F",
                "_status": status,
                "_opened": opened,
            }
        )
    return rows


def gen_task_steps(rng: random.Random, tasks: list[dict], items: list[dict]) -> list[dict]:
    """Step-by-step PM procedures; some steps consume a spare part."""
    rows = []
    for t_i, task in enumerate(tasks):
        n_steps = rng.randint(3, 8)
        for s in range(n_steps):
            use_part = rng.random() < 0.3
            item = items[(t_i * 3 + s) % len(items)] if use_part else None
            rows.append(
                {
                    "task_no": task["task_no"],
                    "proc_seq": (s + 1) * 10,
                    "task_desc": STEP_VERBS[(t_i + s) % len(STEP_VERBS)],
                    "item": item["item"] if item else "",
                    "replaceqty": f"{rng.choice([1, 1, 2, 4])}.00" if item else "",
                }
            )
    return rows


def gen_part_issues(rng: random.Random, work_orders: list[dict], items: list[dict]) -> list[dict]:
    """Historical part issues against closed / in-progress work orders."""
    usable = [w for w in work_orders if w["_status"] in ("H", "IN_PROGRESS", "ON_HOLD")]
    rows = []
    for wo in usable:
        if rng.random() > 0.4:
            continue
        for _ in range(rng.randint(1, 2)):
            item = items[rng.randrange(len(items))]
            rows.append(
                {
                    "wo": wo["wo"],
                    "item": item["item"],
                    "qty": f"{rng.choice([1, 1, 2, 3])}.00",
                    "date_wo": mmddyy(wo["_opened"]),
                    "compid": wo["compid"],
                    "descrip": item["sf_desc"],
                }
            )
    return rows


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic demo CSVs for shopfloor-cmms.")
    ap.add_argument("--out", default="data/demo", help="output directory (default: data/demo)")
    args = ap.parse_args()
    out = Path(args.out)

    rng = random.Random(SEED)
    print(f"generating synthetic demo data (seed={SEED}) into {out}/")

    assets = gen_assets(rng)
    tasks = gen_tasks(rng)
    pms = gen_pm_schedules(rng, assets, tasks)
    items = gen_inventory(rng)
    contacts = gen_contacts(rng)
    work_orders = gen_work_orders(rng, assets)
    steps = gen_task_steps(rng, tasks, items)
    issues = gen_part_issues(rng, work_orders, items)

    a_cols = ["compid", "comp_desc", "assettype", "assetsubtp", "department",
              "line_no", "model_no", "serial_no", "available"]
    write_csv(out / "assets.csv", a_cols,
              [[a[c] for c in a_cols] for a in assets], "utf-8-sig")

    write_csv(out / "tasks.csv", ["task_no", "task_desc"],
              [[t["task_no"], t["task_desc"]] for t in tasks], "utf-8-sig")

    pm_cols = ["pmid", "compid", "task_no", "pmfreqx", "pmfreq", "pmnextdate", "lastpmdate",
               "lastpmno", "dayscmpl", "standard", "estlabor", "assignto", "suppress"]
    write_csv(out / "scheduled_activity.csv", pm_cols,
              [[p[c] for c in pm_cols] for p in pms], "utf-8-sig")

    # inventory.csv: 19 columns + one trailing empty column (the loader expects 20).
    inv_cols = ["item", "asset_sub", "sf_desc", "vpartno", "descrip", "location", "orderpt",
                "onhand", "cost", "lead_time", "obsol", "stock", "supplier", "weblink",
                "photo", "comment", "alt_item", "parnt_item", "child_item"]
    write_csv(out / "inventory.csv", [*inv_cols, ""],
              [[*(it[c] for c in inv_cols), ""] for it in items], "cp1252")

    c_cols = ["contactid", "company", "category", "fname", "lname", "fullname", "email",
              "wphone", "ext", "mobile", "waddress", "wweb"]
    write_csv(out / "contacts.csv", c_cols,
              [[c[col] for col in c_cols] for c in contacts], "latin-1")

    w_cols = ["wo", "compid", "wo_type", "workstatus", "brief_desc", "diag", "comments",
              "date_wo", "sch_date", "time", "time_cmpl", "editdate", "edittime", "edituser",
              "assignto", "miscreated"]
    write_csv(out / "work_orders.csv", w_cols,
              [[w[c] for c in w_cols] for w in work_orders], "latin-1")

    s_cols = ["task_no", "proc_seq", "task_desc", "item", "replaceqty"]
    write_csv(out / "task_steps_parts.csv", s_cols,
              [[s[c] for c in s_cols] for s in steps], "cp1252")

    p_cols = ["wo", "item", "qty", "date_wo", "compid", "descrip"]
    write_csv(out / "part_issues.csv", p_cols,
              [[p[c] for c in p_cols] for p in issues], "cp1252")

    e_cols = ["code", "descr", "station_hint", "recency_status",
              "pdd_class", "source_table", "source_column", "axis"]
    efc_rows = [
        [code, descr, hint, "active",
         EFC_CONSTANTS["pdd_class"], EFC_CONSTANTS["source_table"],
         EFC_CONSTANTS["source_column"], EFC_CONSTANTS["axis"]]
        for code, descr, hint in EFC_CODES
    ]
    write_csv(out / "efc_codes.csv", e_cols, efc_rows, "utf-8")

    print(
        f"done: {len(assets)} assets, {len(tasks)} tasks, {len(pms)} pm schedules, "
        f"{len(items)} items, {len(contacts)} contacts, {len(work_orders)} work orders, "
        f"{len(steps)} task steps, {len(issues)} part issues, {len(efc_rows)} failure codes"
    )


if __name__ == "__main__":
    main()
