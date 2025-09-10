# app.py — Payroll (SQLite + Streamlit) with Google Drive sync + PDF exports
# Row-level Edit/Delete actions + Confirmation panel before delete (no typing EmpID)

import streamlit as st
import sqlite3
import pandas as pd
import io
import os
import json
import time
import calendar
from datetime import date

# PDF
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

# Google Drive
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials

DB_PATH = "payroll.db"
LEAVE_DIVISOR = 30

# Drive globals
GDRIVE_ENABLED = False
GDRIVE_FILE_ID = None
AUTO_BACKUP = False
_drive = None
_last_push_ts = 0
PUSH_COOLDOWN_SEC = 2
_last_drive_error = None

# ------------------ helpers ------------------

def money(n):
    try:
        return f"₹{float(n):,.2f}"
    except Exception:
        return "₹0.00"

def ensure_state_keys():
    for k, v in {
        "editing_emp": None,
        "editing_bonus": None,
        "editing_ded": None,
        "pending_delete": None,  # dict: {'type': 'employee'|'bonus'|'deduction', ...}
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v

# ------------------ drive ------------------

def _drive_init():
    global GDRIVE_ENABLED, GDRIVE_FILE_ID, _drive, AUTO_BACKUP, _last_drive_error
    GDRIVE_ENABLED = False
    _last_drive_error = None
    try:
        cfg = st.secrets.get("gdrive", None)
        if not cfg:
            _last_drive_error = "No [gdrive] in secrets."
            return False
        GDRIVE_FILE_ID = cfg.get("file_id")
        AUTO_BACKUP = bool(cfg.get("auto_backup", False))
        if not GDRIVE_FILE_ID:
            _last_drive_error = "Missing gdrive.file_id."
            return False
        if "service_account_json" in cfg:
            sa_dict = json.loads(cfg.get("service_account_json"))
        elif "service_account" in cfg:
            sa_dict = dict(cfg.get("service_account"))
        else:
            _last_drive_error = "Missing service account credentials."
            return False
        if "private_key" in sa_dict and "\\n" in sa_dict["private_key"]:
            sa_dict["private_key"] = sa_dict["private_key"].replace("\\n", "\n")

        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            sa_dict, scopes=["https://www.googleapis.com/auth/drive"]
        )
        gauth = GoogleAuth()
        gauth.credentials = creds
        drive = GoogleDrive(gauth)
        global _drive
        _drive = drive
        GDRIVE_ENABLED = True
        return True
    except Exception as e:
        _last_drive_error = f"{e}"
        GDRIVE_ENABLED = False
        return False

def drive_pull(local_path=DB_PATH):
    if not GDRIVE_ENABLED:
        return False
    try:
        f = _drive.CreateFile({"id": GDRIVE_FILE_ID})
        f.GetContentFile(local_path)
        return True
    except Exception as e:
        global _last_drive_error
        _last_drive_error = f"Drive pull failed: {e}"
        return False

def drive_push(local_path=DB_PATH):
    if not GDRIVE_ENABLED:
        return False
    global _last_push_ts, _last_drive_error
    if time.time() - _last_push_ts < PUSH_COOLDOWN_SEC:
        return False
    try:
        f = _drive.CreateFile({"id": GDRIVE_FILE_ID})
        f.SetContentFile(local_path)
        f.Upload()
        _last_push_ts = time.time()
        return True
    except Exception as e:
        _last_drive_error = f"Drive push failed: {e}"
        return False

# ------------------ sqlite ------------------

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                emp_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                role TEXT,
                salary_type TEXT CHECK(salary_type IN ('Monthly','PerDay')) NOT NULL,
                monthly_salary REAL,
                per_day_rate REAL,
                doj TEXT,
                active INTEGER DEFAULT 1,
                bank TEXT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                emp_id TEXT NOT NULL,
                status TEXT CHECK(status IN ('Present','Absent','Half Day','Weekly Off')) NOT NULL,
                note TEXT,
                FOREIGN KEY(emp_id) REFERENCES employees(emp_id)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS deductions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                emp_id TEXT NOT NULL,
                dtype TEXT CHECK(dtype IN ('Advance','Other')) NOT NULL,
                amount REAL NOT NULL,
                note TEXT,
                FOREIGN KEY(emp_id) REFERENCES employees(emp_id)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bonuses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                emp_id TEXT NOT NULL,
                amount REAL NOT NULL,
                note TEXT,
                FOREIGN KEY(emp_id) REFERENCES employees(emp_id)
            );
        """)
        conn.commit()

def month_bounds(year, month):
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start, end

def df_from_query(sql, params=()):
    with get_conn() as conn:
        return pd.read_sql_query(sql, conn, params=params)

def execute(sql, params=()):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
    if GDRIVE_ENABLED and AUTO_BACKUP:
        drive_push(DB_PATH)

# ------------------ DELETE CONFIRMATION PANEL ------------------

def render_delete_confirmation():
    """
    Shows a confirmation panel when st.session_state.pending_delete is set.
    Expected payloads:
      - Employee: {'type':'employee', 'emp_id':..., 'name':..., 'role':..., 'active':..., 'counts':{'att':n,'bon':n,'ded':n}}
      - Bonus:    {'type':'bonus', 'id':..., 'day':..., 'emp_id':..., 'amount':..., 'note':...}
      - Deduction:{'type':'deduction', 'id':..., 'day':..., 'emp_id':..., 'dtype':..., 'amount':..., 'note':...}
    """
    pdp = st.session_state.get("pending_delete")
    if not pdp:
        return

    st.markdown("---")
    with st.container():
        st.error("⚠️ Please confirm deletion")
        if pdp["type"] == "employee":
            info = pdp
            c1, c2, c3, c4 = st.columns(4)
            c1.write(f"**EmpID:** {info['emp_id']}")
            c2.write(f"**Name:** {info['name']}")
            c3.write(f"**Role:** {info.get('role') or ''}")
            c4.write(f"**Active:** {'Yes' if info.get('active') else 'No'}")
            cnt = info["counts"]
            st.write(
                f"This will permanently delete the employee and all related data: "
                f"**Attendance: {cnt['att']}**, **Bonuses: {cnt['bon']}**, **Deductions: {cnt['ded']}**."
            )
            cc1, cc2 = st.columns(2)
            if cc1.button("✅ Confirm Delete (Employee + all related)"):
                # cascade delete
                execute("DELETE FROM attendance WHERE emp_id=?", (info['emp_id'],))
                execute("DELETE FROM bonuses    WHERE emp_id=?", (info['emp_id'],))
                execute("DELETE FROM deductions WHERE emp_id=?", (info['emp_id'],))
                execute("DELETE FROM employees  WHERE emp_id=?", (info['emp_id'],))
                st.success(f"Deleted {info['emp_id']} — {info['name']} and all related data.")
                st.session_state.pending_delete = None
                st.experimental_rerun()
            if cc2.button("❌ Cancel"):
                st.session_state.pending_delete = None
                st.info("Cancelled.")

        elif pdp["type"] == "bonus":
            r = pdp
            st.write(f"**Bonus ID:** {r['id']}  |  **Date:** {r['day']}  |  **EmpID:** {r['emp_id']}")
            st.write(f"**Amount:** {money(r['amount'])}  |  **Note:** {r.get('note') or ''}")
            cc1, cc2 = st.columns(2)
            if cc1.button("✅ Confirm Delete (Bonus)"):
                execute("DELETE FROM bonuses WHERE id=?", (int(r["id"]),))
                st.success("Bonus deleted.")
                st.session_state.pending_delete = None
                st.experimental_rerun()
            if cc2.button("❌ Cancel"):
                st.session_state.pending_delete = None
                st.info("Cancelled.")

        elif pdp["type"] == "deduction":
            r = pdp
            st.write(f"**Deduction ID:** {r['id']}  |  **Date:** {r['day']}  |  **EmpID:** {r['emp_id']}")
            st.write(f"**Type:** {r['dtype']}  |  **Amount:** {money(r['amount'])}  |  **Note:** {r.get('note') or ''}")
            cc1, cc2 = st.columns(2)
            if cc1.button("✅ Confirm Delete (Deduction)"):
                execute("DELETE FROM deductions WHERE id=?", (int(r["id"]),))
                st.success("Deduction deleted.")
                st.session_state.pending_delete = None
                st.experimental_rerun()
            if cc2.button("❌ Cancel"):
                st.session_state.pending_delete = None
                st.info("Cancelled.")

    st.markdown("---")

# ------------------ EMPLOYEES (add + row actions) ------------------

def _employee_row_actions(df):
    if df.empty:
        st.info("No employees.")
        return

    st.markdown("### Employees List")
    header = st.columns([1.4, 2.6, 1.2, 1.2, 1.0, 0.9, 1.2])  # + Actions wider
    for i, h in enumerate(["EmpID", "Name", "Type", "Base/Rate", "Role", "Active", "Actions"]):
        header[i].markdown(f"**{h}**")

    for _, r in df.iterrows():
        col = st.columns([1.4, 2.6, 1.2, 1.2, 1.0, 0.9, 1.2])
        col[0].write(r.emp_id)
        col[1].write(r.name)
        col[2].write(r.salary_type)
        base_rate = r.monthly_salary if r.salary_type == "Monthly" else r.per_day_rate
        col[3].write(money(base_rate or 0))
        col[4].write(r.role if pd.notna(r.role) else "")
        col[5].write("✅" if r.active else "—")

        e_key = f"emp_edit_{r.emp_id}"
        d_key = f"emp_del_{r.emp_id}"
        c_edit, c_del = col[6].columns(2)
        edit_clicked = c_edit.button("✏️", key=e_key, help="Edit")
        del_clicked  = c_del.button("🗑", key=d_key, help="Delete")

        if edit_clicked:
            st.session_state.editing_emp = r.emp_id

        if del_clicked:
            # Build confirmation payload with counts
            cnt_att = df_from_query("SELECT COUNT(*) AS c FROM attendance WHERE emp_id=?", (r.emp_id,)).iloc[0]["c"]
            cnt_bon = df_from_query("SELECT COUNT(*) AS c FROM bonuses WHERE emp_id=?",   (r.emp_id,)).iloc[0]["c"]
            cnt_ded = df_from_query("SELECT COUNT(*) AS c FROM deductions WHERE emp_id=?", (r.emp_id,)).iloc[0]["c"]
            st.session_state.pending_delete = {
                "type": "employee",
                "emp_id": r.emp_id,
                "name": r.name,
                "role": r.role if pd.notna(r.role) else "",
                "active": bool(r.active),
                "counts": {"att": int(cnt_att), "bon": int(cnt_bon), "ded": int(cnt_ded)},
            }

        # inline edit form
        if st.session_state.editing_emp == r.emp_id:
            with st.form(f"edit_emp_form_{r.emp_id}"):
                c1, c2, c3 = st.columns(3)
                name   = c1.text_input("Name *", value=r.name)
                role   = c2.text_input("Role", value=r.role if pd.notna(r.role) else "")
                active = c3.checkbox("Active", value=bool(r.active))

                d1, d2, d3 = st.columns(3)
                salary_type = d1.selectbox("Salary Type *", ["Monthly","PerDay"],
                                           index=0 if r.salary_type=="Monthly" else 1)
                monthly_salary = d2.number_input("Monthly Salary", min_value=0.0, step=100.0, format="%.2f",
                                                 value=float(r.monthly_salary or 0))
                per_day_rate   = d3.number_input("Per-Day Rate", min_value=0.0, step=10.0, format="%.2f",
                                                 value=float(r.per_day_rate or 0))
                e1, e2 = st.columns(2)
                doj  = e1.date_input("Date of Joining",
                                     value=(pd.to_datetime(r.doj).date() if pd.notna(r.doj) and str(r.doj)!=""
                                            else date.today()))
                bank = e2.text_input("Bank / UPI", value=r.bank if pd.notna(r.bank) else "")
                save = st.form_submit_button("💾 Save")
            if save:
                execute("""
                    UPDATE employees SET
                        name=?, role=?, salary_type=?, monthly_salary=?, per_day_rate=?, doj=?, active=?, bank=?
                    WHERE emp_id=?
                """, (name, role, salary_type,
                      monthly_salary if salary_type=="Monthly" else None,
                      per_day_rate   if salary_type=="PerDay"  else None,
                      doj.isoformat(), 1 if active else 0, bank, r.emp_id))
                st.success("Updated.")
                st.session_state.editing_emp = None
                st.experimental_rerun()

def ui_employees():
    st.subheader("Employees")

    with st.form("add_emp"):
        st.markdown("#### ➕ Add Employee")
        c1, c2, c3 = st.columns(3)
        emp_id = c1.text_input("Emp ID *")
        name   = c2.text_input("Name *")
        role   = c3.text_input("Role")
        d1, d2, d3 = st.columns(3)
        salary_type = d1.selectbox("Salary Type *", ["Monthly","PerDay"])
        monthly_salary = d2.number_input("Monthly Salary", min_value=0.0, step=100.0, format="%.2f")
        per_day_rate   = d3.number_input("Per-Day Rate",   min_value=0.0, step=10.0,  format="%.2f")
        e1, e2, e3 = st.columns(3)
        doj    = e1.date_input("Date of Joining", value=date.today())
        active = e2.checkbox("Active", value=True)
        bank   = e3.text_input("Bank / UPI")
        if st.form_submit_button("Save Employee"):
            if not emp_id or not name:
                st.error("Emp ID and Name are required")
            else:
                execute("""
                    INSERT INTO employees(emp_id,name,role,salary_type,monthly_salary,per_day_rate,doj,active,bank)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(emp_id) DO UPDATE SET
                        name=excluded.name, role=excluded.role, salary_type=excluded.salary_type,
                        monthly_salary=excluded.monthly_salary, per_day_rate=excluded.per_day_rate,
                        doj=excluded.doj, active=excluded.active, bank=excluded.bank
                """, (emp_id, name, role, salary_type,
                      monthly_salary if salary_type=="Monthly" else None,
                      per_day_rate   if salary_type=="PerDay"  else None,
                      doj.isoformat(), 1 if active else 0, bank))
                st.success("Employee saved/updated.")

    df = df_from_query("""
        SELECT emp_id,name,role,salary_type,monthly_salary,per_day_rate,doj,active,bank
        FROM employees ORDER BY emp_id
    """)
    _employee_row_actions(df)

# ------------------ ATTENDANCE ------------------

def ui_attendance():
    st.subheader("Attendance (Single Entry)")
    ids = df_from_query("SELECT emp_id FROM employees WHERE active=1 ORDER BY emp_id")
    emp_ids = [r["emp_id"] for _, r in ids.iterrows()]
    with st.form("add_att"):
        d, e, s, n = st.columns(4)
        day = d.date_input("Date", value=date.today())
        emp = e.selectbox("Emp ID", emp_ids)
        status = s.selectbox("Status", ["Present","Absent","Half Day","Weekly Off"])
        note = n.text_input("Note")
        if st.form_submit_button("Save Attendance"):
            execute("INSERT INTO attendance(day,emp_id,status,note) VALUES(?,?,?,?)",
                    (day.isoformat(), emp, status, note))
            st.success("Saved.")
    st.markdown("---")
    y = st.number_input("Year", 2000, 2100, date.today().year)
    m = st.number_input("Month", 1, 12, date.today().month)
    start, end = month_bounds(int(y), int(m))
    st.dataframe(df_from_query(
        "SELECT day,emp_id,status,note FROM attendance WHERE day>=? AND day<? ORDER BY day,emp_id",
        (start.isoformat(), end.isoformat())
    ), use_container_width=True)

def ui_attendance_calendar():
    st.subheader("Attendance (Calendar — click to set)")
    emps = df_from_query("SELECT emp_id, name FROM employees WHERE active=1 ORDER BY emp_id")
    if emps.empty:
        st.info("Add an employee first.")
        return

    emp_map = {f"{r['emp_id']} — {r['name']}": r['emp_id'] for _, r in emps.iterrows()}
    c1, c2, c3, c4 = st.columns(4)
    emp_label = c1.selectbox("Employee", list(emp_map.keys()))
    year = int(c2.number_input("Year", 2000, 2100, date.today().year))
    month = int(c3.number_input("Month", 1, 12, date.today().month))
    overwrite = c4.checkbox("Overwrite existing when saving", value=True)

    emp_id = emp_map[emp_label]
    start, end = month_bounds(year, month)
    firstweekday = 0
    cal = calendar.Calendar(firstweekday=firstweekday)

    existing = df_from_query(
        "SELECT day, status FROM attendance WHERE emp_id=? AND day>=? AND day<?",
        (emp_id, start.isoformat(), end.isoformat())
    )
    existing_map = {int(pd.to_datetime(d).day): s for d, s in zip(existing["day"], existing["status"])}

    sess_key = f"calmap::{emp_id}::{year}-{month:02d}"
    if sess_key not in st.session_state:
        days_in_month = calendar.monthrange(year, month)[1]
        st.session_state[sess_key] = {d: existing_map.get(d) for d in range(1, days_in_month+1)}
    calmap = st.session_state[sess_key]

    cycle = ["Present","Absent","Half Day","Weekly Off",None]
    code = {"Present":"P","Absent":"A","Half Day":"H","Weekly Off":"O",None:" "}

    wd_names = list(calendar.day_abbr)
    if firstweekday != 0:
        wd_names = wd_names[firstweekday:] + wd_names[:firstweekday]
    hdr = st.columns(7)
    for i, name in enumerate(wd_names):
        hdr[i].markdown(f"**{name}**")

    for week in cal.monthdayscalendar(year, month):
        cols_w = st.columns(7)
        for i, d in enumerate(week):
            if d == 0:
                cols_w[i].markdown("&nbsp;")
            else:
                label = f"{d:02d} [{code.get(calmap.get(d), ' ')}]"
                if cols_w[i].button(label, key=f"{sess_key}::{d}"):
                    cur = calmap.get(d)
                    idx = cycle.index(cur) if cur in cycle else len(cycle)-1
                    calmap[d] = cycle[(idx+1)%len(cycle)]

    st.markdown("---")
    st.write("Quick range fill:")
    r1, r2, r3 = st.columns(3)
    days_in_month = calendar.monthrange(year, month)[1]
    d_start = int(r1.number_input("Start", 1, days_in_month, 1))
    d_end   = int(r2.number_input("End",   1, days_in_month, days_in_month))
    rstatus = r3.selectbox("Status", ["Present","Absent","Half Day","Weekly Off"])
    if st.button("Apply Range"):
        a,b = min(d_start,d_end), max(d_start,d_end)
        for d in range(a,b+1): calmap[d]=rstatus
        st.success(f"Applied {rstatus} to {a}-{b}.")
    if st.button("Fill empty as Present"):
        for d in calmap:
            if calmap[d] is None: calmap[d]="Present"
        st.success("Filled unset days as Present.")

    if st.button("💾 Save to database"):
        with get_conn() as conn:
            cur = conn.cursor()
            for d, s in calmap.items():
                dstr = date(year, month, d).isoformat()
                if s is None:
                    if overwrite:
                        cur.execute("DELETE FROM attendance WHERE emp_id=? AND day=?", (emp_id, dstr))
                    continue
                if overwrite:
                    cur.execute("DELETE FROM attendance WHERE emp_id=? AND day=?", (emp_id, dstr))
                cur.execute("INSERT OR IGNORE INTO attendance(day,emp_id,status,note) VALUES(?,?,?,?)",
                            (dstr, emp_id, s, "calendar"))
            conn.commit()
        st.success("Saved.")
        if GDRIVE_ENABLED and AUTO_BACKUP:
            drive_push(DB_PATH)

# ------------------ BONUSES (row actions + confirm) ------------------

def _row_actions_bonuses(df):
    st.markdown("### Bonus Entries")
    if df.empty:
        st.info("No bonuses yet.")
        return

    hdr = st.columns([1.0, 1.0, 1.2, 2.0, 1.2])
    for i, h in enumerate(["ID","Date","EmpID","Note / Amount","Actions"]):
        hdr[i].markdown(f"**{h}**")

    for _, r in df.iterrows():
        cols = st.columns([1.0,1.0,1.2,2.0,1.2])
        cols[0].write(r.id)
        cols[1].write(r.day)
        cols[2].write(r.emp_id)
        cols[3].write(f"{r.note if pd.notna(r.note) else ''}  —  {money(r.amount)}")

        e_btn = cols[4].button("✏️", key=f"bon_edit_{r.id}", help="Edit")
        d_btn = cols[4].button("🗑", key=f"bon_del_{r.id}", help="Delete")

        if e_btn:
            st.session_state.editing_bonus = int(r.id)

        if d_btn:
            st.session_state.pending_delete = {
                "type": "bonus",
                "id": int(r.id),
                "day": r.day,
                "emp_id": r.emp_id,
                "amount": float(r.amount),
                "note": r.note if pd.notna(r.note) else "",
            }

        if st.session_state.editing_bonus == int(r.id):
            with st.form(f"bon_edit_form_{r.id}"):
                c1,c2,c3 = st.columns(3)
                day    = c1.date_input("Date", pd.to_datetime(r.day).date())
                emp    = c2.text_input("Emp ID", value=r.emp_id)
                amount = c3.number_input("Amount", min_value=0.0, step=100.0, format="%.2f",
                                         value=float(r.amount))
                note   = st.text_input("Note", value=r.note if pd.notna(r.note) else "")
                save   = st.form_submit_button("💾 Save")
            if save:
                execute("UPDATE bonuses SET day=?, emp_id=?, amount=?, note=? WHERE id=?",
                        (day.isoformat(), emp, amount, note, int(r.id)))
                st.success("Updated.")
                st.session_state.editing_bonus = None
                st.experimental_rerun()

def ui_bonuses():
    st.subheader("Bonuses")
    with st.form("add_bonus"):
        st.markdown("#### ➕ Add Bonus")
        ids = df_from_query("SELECT emp_id FROM employees WHERE active=1 ORDER BY emp_id")
        emp_ids = [r["emp_id"] for _, r in ids.iterrows()]
        c1, c2, c3 = st.columns(3)
        day    = c1.date_input("Date", date.today())
        emp    = c2.selectbox("Emp ID", emp_ids)
        amount = c3.number_input("Amount", min_value=0.0, step=100.0, format="%.2f")
        note   = st.text_input("Note")
        if st.form_submit_button("Save Bonus"):
            execute("INSERT INTO bonuses(day,emp_id,amount,note) VALUES(?,?,?,?)",
                    (day.isoformat(), emp, amount, note))
            st.success("Saved.")
    df = df_from_query("SELECT id, day, emp_id, amount, note FROM bonuses ORDER BY day DESC, id DESC")
    _row_actions_bonuses(df)

# ------------------ DEDUCTIONS (row actions + confirm) ------------------

def _row_actions_deductions(df):
    st.markdown("### Deduction Entries")
    if df.empty:
        st.info("No deductions yet.")
        return

    hdr = st.columns([1.0, 1.0, 1.2, 1.0, 2.0, 1.2])
    for i, h in enumerate(["ID","Date","EmpID","Type","Note / Amount","Actions"]):
        hdr[i].markdown(f"**{h}**")

    for _, r in df.iterrows():
        cols = st.columns([1.0,1.0,1.2,1.0,2.0,1.2])
        cols[0].write(r.id)
        cols[1].write(r.day)
        cols[2].write(r.emp_id)
        cols[3].write(r.dtype)
        cols[4].write(f"{r.note if pd.notna(r.note) else ''}  —  {money(r.amount)}")

        e_btn = cols[5].button("✏️", key=f"ded_edit_{r.id}", help="Edit")
        d_btn = cols[5].button("🗑", key=f"ded_del_{r.id}", help="Delete")

        if e_btn:
            st.session_state.editing_ded = int(r.id)

        if d_btn:
            st.session_state.pending_delete = {
                "type": "deduction",
                "id": int(r.id),
                "day": r.day,
                "emp_id": r.emp_id,
                "dtype": r.dtype,
                "amount": float(r.amount),
                "note": r.note if pd.notna(r.note) else "",
            }

        if st.session_state.editing_ded == int(r.id):
            with st.form(f"ded_edit_form_{r.id}"):
                c1,c2,c3,c4 = st.columns(4)
                day    = c1.date_input("Date", pd.to_datetime(r.day).date())
                emp    = c2.text_input("Emp ID", value=r.emp_id)
                dtype  = c3.selectbox("Type", ["Advance","Other"], index=0 if r.dtype=="Advance" else 1)
                amount = c4.number_input("Amount", min_value=0.0, step=100.0, format="%.2f",
                                         value=float(r.amount))
                note   = st.text_input("Note", value=r.note if pd.notna(r.note) else "")
                save   = st.form_submit_button("💾 Save")
            if save:
                execute("UPDATE deductions SET day=?, emp_id=?, dtype=?, amount=?, note=? WHERE id=?",
                        (day.isoformat(), emp, dtype, amount, note, int(r.id)))
                st.success("Updated.")
                st.session_state.editing_ded = None
                st.experimental_rerun()

def ui_deductions():
    st.subheader("Deductions")
    with st.form("add_ded"):
        st.markdown("#### ➕ Add Deduction")
        ids = df_from_query("SELECT emp_id FROM employees WHERE active=1 ORDER BY emp_id")
        emp_ids = [r["emp_id"] for _, r in ids.iterrows()]
        c1, c2, c3 = st.columns(3)
        day  = c1.date_input("Date", date.today())
        emp  = c2.selectbox("Emp ID", emp_ids)
        dtype = c3.selectbox("Type", ["Advance","Other"])
        amount = st.number_input("Amount", min_value=0.0, step=100.0, format="%.2f")
        note   = st.text_input("Note")
        if st.form_submit_button("Save Deduction"):
            execute("INSERT INTO deductions(day,emp_id,dtype,amount,note) VALUES(?,?,?,?,?)",
                    (day.isoformat(), emp, dtype, amount, note))
            st.success("Saved.")
    df = df_from_query("SELECT id, day, emp_id, dtype, amount, note FROM deductions ORDER BY day DESC, id DESC")
    _row_actions_deductions(df)

# ------------------ PAYROLL + PDF ------------------

def payroll_df(year, month):
    start, end = month_bounds(year, month)
    emps = df_from_query("""
        SELECT emp_id,name,role,salary_type,monthly_salary,per_day_rate
        FROM employees WHERE active=1
    """)
    att = df_from_query("""
        SELECT emp_id,
               SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) AS present,
               SUM(CASE WHEN status='Half Day' THEN 1 ELSE 0 END) AS half_day,
               SUM(CASE WHEN status='Absent' THEN 1 ELSE 0 END) AS absent
        FROM attendance WHERE day>=? AND day<? GROUP BY emp_id
    """, (start.isoformat(), end.isoformat()))
    bon = df_from_query("""
        SELECT emp_id, COALESCE(SUM(amount),0) AS bonus
        FROM bonuses WHERE day>=? AND day<? GROUP BY emp_id
    """, (start.isoformat(), end.isoformat()))
    ded = df_from_query("""
        SELECT emp_id, COALESCE(SUM(amount),0) AS deduction
        FROM deductions WHERE day>=? AND day<? GROUP BY emp_id
    """, (start.isoformat(), end.isoformat()))

    df = emps.merge(att, on="emp_id", how="left") \
             .merge(bon, on="emp_id", how="left") \
             .merge(ded, on="emp_id", how="left")
    df[["present","half_day","absent","bonus","deduction"]] = df[
        ["present","half_day","absent","bonus","deduction"]
    ].fillna(0)

    rows = []
    for _, r in df.iterrows():
        if r["salary_type"] == "Monthly":
            base  = float(r["monthly_salary"] or 0)
            leave = (base / LEAVE_DIVISOR) * (r["absent"] + 0.5 * r["half_day"])
        else:
            rate  = float(r["per_day_rate"] or 0)
            base  = (r["present"] + 0.5 * r["half_day"]) * rate
            leave = 0.0
        gross = base - leave + float(r["bonus"])
        net   = gross - float(r["deduction"])
        rows.append([
            r["emp_id"], r["name"], r["salary_type"],
            int(r["present"]), int(r["half_day"]), int(r["absent"]),
            round(base,2), round(leave,2), round(float(r["bonus"]),2), round(float(r["deduction"]),2),
            round(gross,2), round(net,2)
        ])
    return pd.DataFrame(rows, columns=[
        "EmpID","Name","Type","Present","Half","Absent","Base","LeaveDed","Bonus","Deduction","Gross","Net"
    ])

def pdf_payroll(df, year, month) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w,h = A4
    x = 12*mm
    y0 = h - 18*mm
    row_h = 6.5*mm

    def header():
        c.setFont("Helvetica-Bold", 13)
        c.drawString(x, y0+6*mm, f"Payroll Summary — {year}-{month:02d}")
        c.setFont("Helvetica-Bold", 9)
        y = y0
        cols=["EmpID","Name","Type","P","H","A","Base","Leave","Bonus","Ded","Gross","Net"]
        x_pos=[x,x+20*mm,x+70*mm,x+92*mm,x+100*mm,x+108*mm,x+116*mm,x+136*mm,
               x+156*mm,x+176*mm,x+196*mm,x+216*mm]
        for col,xi in zip(cols,x_pos): c.drawString(xi,y,col)
        return y-row_h, x_pos

    y, x_pos = header()
    c.setFont("Helvetica", 9)
    for _, r in df.iterrows():
        if y < 20*mm:
            c.showPage()
            y, x_pos = header()
            c.setFont("Helvetica",9)
        vals=[r["EmpID"], str(r["Name"])[:20], r["Type"], r["Present"], r["Half"], r["Absent"],
              money(r["Base"]), money(r["LeaveDed"]), money(r["Bonus"]), money(r["Deduction"]),
              money(r["Gross"]), money(r["Net"])]
        for val,xi in zip(vals,x_pos): c.drawString(xi,y,str(val))
        y-=row_h
    c.setFont("Helvetica-Bold",10)
    c.drawString(x, y-4*mm, f"Total Net: {money(df['Net'].sum())}")
    c.showPage(); c.save()
    return buf.getvalue()

def pdf_payslip(emp_label, erow, start_date, end_date, present, half_day, absent,
                base, leave, bonuses_df, deductions_df, net) -> bytes:
    buf=io.BytesIO(); c=canvas.Canvas(buf, pagesize=A4); w,h=A4
    x=20*mm; y=h-20*mm
    def line(t, inc=7*mm, bold=False):
        nonlocal y; c.setFont("Helvetica-Bold" if bold else "Helvetica", 11); c.drawString(x,y,t); y-=inc
    c.setFont("Helvetica-Bold",14); c.drawString(x,y,"Payslip"); y-=10*mm
    line(f"Employee: {emp_label}"); line(f"Period: {start_date} to {end_date}"); line(f"Salary Type: {erow.salary_type}"); line("")
    line("Attendance", bold=True); line(f"Present: {present}   Half-day: {half_day}   Absent: {absent}"); line("")
    line("Amounts", bold=True); line(f"Base Pay: {money(base)}"); line(f"Leave Deduction: {money(leave)}")
    line(""); line("Bonuses (itemized)", bold=True)
    if bonuses_df.empty: line("  None")
    else:
        for _,r in bonuses_df.iterrows():
            note=f" — {r['note']}" if pd.notna(r['note']) and r['note'] else ""
            line(f"  {r['day']}: {money(r['amount'])}{note}")
    line(""); line("Deductions (itemized)", bold=True)
    if deductions_df.empty: line("  None")
    else:
        for _,r in deductions_df.iterrows():
            note=f" — {r['note']}" if pd.notna(r['note']) and r['note'] else ""
            line(f"  {r['day']} [{r['dtype']}]: {money(r['amount'])}{note}")
    line(""); line(f"Net Payable: {money(net)}", bold=True)
    c.showPage(); c.save(); return buf.getvalue()

def ui_payroll():
    st.subheader("Payroll")
    c1,c2,c3 = st.columns(3)
    year = int(c1.number_input("Year", 2000, 2100, date.today().year))
    month= int(c2.number_input("Month", 1, 12, date.today().month))
    if c3.button("Calculate"):
        df = payroll_df(year, month)
        st.dataframe(df, use_container_width=True)
        st.metric("Total Net", money(df["Net"].sum()))
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Download Payroll CSV", data=csv,
                           file_name=f"payroll_{year}_{month:02d}.csv", mime="text/csv")
        xout = io.BytesIO()
        with pd.ExcelWriter(xout, engine="xlsxwriter") as w:
            df.to_excel(w, index=False, sheet_name="Payroll")
        st.download_button("Download Payroll Excel", data=xout.getvalue(),
                           file_name=f"payroll_{year}_{month:02d}.xlsx")
        pdf = pdf_payroll(df, year, month)
        st.download_button("Download Payroll PDF", data=pdf,
                           file_name=f"payroll_{year}_{month:02d}.pdf", mime="application/pdf")

def ui_payslip():
    st.subheader("Payslip (Itemized)")
    emps = df_from_query("""
        SELECT emp_id, name, salary_type, monthly_salary, per_day_rate
        FROM employees WHERE active=1 ORDER BY emp_id
    """)
    if emps.empty:
        st.info("Add an employee first."); return
    emap = {f"{r['emp_id']} — {r['name']}": r['emp_id'] for _, r in emps.iterrows()}
    c1,c2,c3 = st.columns(3)
    emp_label  = c1.selectbox("Employee", list(emap.keys()))
    start_date = c2.date_input("From", value=date(date.today().year, date.today().month, 1))
    end_date   = c3.date_input("To",   value=date.today())
    if st.button("Generate payslip"):
        emp_id = emap[emp_label]
        erow   = emps[emps.emp_id==emp_id].iloc[0]
        att = df_from_query("""
            SELECT SUM(CASE WHEN status='Present' THEN 1 ELSE 0 END) AS present,
                   SUM(CASE WHEN status='Half Day' THEN 1 ELSE 0 END) AS half_day,
                   SUM(CASE WHEN status='Absent'  THEN 1 ELSE 0 END) AS absent
            FROM attendance WHERE emp_id=? AND day>=? AND day<=?
        """,(emp_id, start_date.isoformat(), end_date.isoformat()))
        present  = int(att.iloc[0]["present"] or 0)
        half_day = int(att.iloc[0]["half_day"] or 0)
        absent   = int(att.iloc[0]["absent"]  or 0)
        bon = df_from_query("""
            SELECT day, amount, note FROM bonuses
            WHERE emp_id=? AND day>=? AND day<=? ORDER BY day
        """,(emp_id, start_date.isoformat(), end_date.isoformat()))
        ded = df_from_query("""
            SELECT day, dtype, amount, note FROM deductions
            WHERE emp_id=? AND day>=? AND day<=? ORDER BY day
        """,(emp_id, start_date.isoformat(), end_date.isoformat()))
        sum_bonus = float(bon["amount"].sum()) if not bon.empty else 0.0
        sum_ded   = float(ded["amount"].sum()) if not ded.empty else 0.0
        if erow.salary_type=="Monthly":
            base=float(erow.monthly_salary or 0); leave=(base/LEAVE_DIVISOR)*(absent+0.5*half_day)
        else:
            rate=float(erow.per_day_rate or 0); base=rate*(present+0.5*half_day); leave=0.0
        gross = base - leave + sum_bonus; net = gross - sum_ded
        st.markdown(f"### {emp_label}"); st.caption(f"Period: **{start_date} → {end_date}**")
        cA,cB,cC,cD = st.columns(4)
        cA.metric("Present",present); cB.metric("Half Day",half_day); cC.metric("Absent",absent); cD.metric("Base",money(base))
        cE,cF,cG = st.columns(3)
        cE.metric("Leave Deduction",money(leave)); cF.metric("Total Bonuses",money(sum_bonus)); cG.metric("Total Deductions",money(sum_ded))
        st.subheader(f"**Net Payable: {money(net)}**")
        st.markdown("#### Bonuses (itemized)")
        st.dataframe(bon.rename(columns={"day":"Date","amount":"Amount","note":"Note"}) if not bon.empty else pd.DataFrame(), use_container_width=True)
        st.markdown("#### Deductions (itemized)")
        st.dataframe(ded.rename(columns={"day":"Date","dtype":"Type","amount":"Amount","note":"Note"}) if not ded.empty else pd.DataFrame(), use_container_width=True)
        slip = pd.DataFrame([{
            "EmpID": emp_id, "Name": emp_label.split(" — ",1)[1],
            "From": start_date, "To": end_date, "Type": erow.salary_type,
            "Present":present, "Half Day":half_day, "Absent":absent,
            "Base":round(base,2), "LeaveDeduction":round(leave,2),
            "Bonuses":round(sum_bonus,2), "Deductions":round(sum_ded,2), "Net":round(net,2)
        }])
        st.download_button("Download Payslip Summary (CSV)",
                           data=slip.to_csv(index=False).encode("utf-8"),
                           file_name=f"payslip_{emp_id}_{start_date}_{end_date}.csv", mime="text/csv")
        pdf = pdf_payslip(emp_label, erow, start_date, end_date, present, half_day, absent, base, leave, bon, ded, net)
        st.download_button("Download Payslip PDF", data=pdf,
                           file_name=f"payslip_{emp_id}_{start_date}_{end_date}.pdf", mime="application/pdf")

# ------------------ MAIN ------------------

def main():
    st.set_page_config(page_title="Payroll App (Drive + PDF + Confirm Delete)", layout="wide")
    ensure_state_keys()

    # Drive controls
    init_ok = _drive_init()
    with st.sidebar.expander("Cloud Sync (Google Drive)"):
        try:
            gdr = st.secrets.get("gdrive", {})
            st.caption(f"DEBUG → gdrive keys: {list(gdr.keys())}")
            st.caption(f"DEBUG → has service_account: {'service_account' in gdr}")
            st.caption(f"DEBUG → has file_id: {bool(gdr.get('file_id'))}")
        except Exception as _e:
            st.caption(f"DEBUG → cannot read secrets: {_e}")

        if init_ok:
            if drive_pull(DB_PATH): st.success("Restored DB from Drive (startup)")
            else:
                st.info("Using local DB (no Drive restore)")
                if _last_drive_error: st.caption(_last_drive_error)

            c1,c2,c3 = st.columns(3)
            if c1.button("⤴ Backup now"):
                st.success("Backed up to Drive" if drive_push(DB_PATH) else (_last_drive_error or "Backup failed"))
            if c2.button("⤵ Restore now"):
                st.success("Restored DB from Drive (manual)" if drive_pull(DB_PATH) else (_last_drive_error or "Restore failed"))
            if os.path.exists(DB_PATH):
                with open(DB_PATH,"rb") as f:
                    st.download_button("Download DB (payroll.db)", f, file_name="payroll.db")
            st.caption(f"AUTO_BACKUP: {'ON' if AUTO_BACKUP else 'OFF'} (set in secrets)")
        else:
            st.error("Drive init failed — using local DB.")
            if _last_drive_error: st.caption(f"Reason: {_last_drive_error}")
            st.caption("Share the Drive file with your service account (Editor).")

    init_db()

    st.title("💼 Payroll App")
    st.caption("Employees • Attendance • Calendar • Bonuses • Deductions • Payroll • Payslip • Drive backup")

    # Global delete confirmation panel (always render near top)
    render_delete_confirmation()

    section = st.sidebar.radio(
        "Go to",
        ["Employees","Attendance","Attendance (Calendar)","Bonuses","Deductions","Payroll","Payslip"]
    )
    if section=="Employees":   ui_employees()
    elif section=="Attendance": ui_attendance()
    elif section=="Attendance (Calendar)": ui_attendance_calendar()
    elif section=="Bonuses":    ui_bonuses()
    elif section=="Deductions": ui_deductions()
    elif section=="Payroll":    ui_payroll()
    elif section=="Payslip":    ui_payslip()

if __name__ == "__main__":
    main()
