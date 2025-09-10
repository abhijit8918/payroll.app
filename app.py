# app.py — Payroll (Streamlit + SQLite) with Google Drive + PDF exports
# ---------------------------------------------------------------------
# Requirements:
#   streamlit, pandas, xlsxwriter, pydrive2, google-auth, google-auth-httplib2,
#   google-api-python-client, oauth2client, reportlab
#
# Secrets examples are the same as in the previous version (see [gdrive] notes).

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
_last_drive_error = None  # for display


# ------------------ Helpers ------------------

def money(n):
    try:
        return f"₹{float(n):,.2f}"
    except Exception:
        return "₹0.00"


# ------------------ Google Drive helpers ------------------

def _drive_init():
    global GDRIVE_ENABLED, GDRIVE_FILE_ID, _drive, AUTO_BACKUP, _last_drive_error
    GDRIVE_ENABLED = False
    _last_drive_error = None
    try:
        cfg = st.secrets.get("gdrive", None)
        if not cfg:
            _last_drive_error = "No [gdrive] section found in secrets."
            return False

        GDRIVE_FILE_ID = cfg.get("file_id")
        AUTO_BACKUP = bool(cfg.get("auto_backup", False))
        if not GDRIVE_FILE_ID:
            _last_drive_error = "Missing gdrive.file_id in secrets."
            return False

        # service account dict
        if "service_account_json" in cfg:
            sa_dict = json.loads(cfg.get("service_account_json"))
        elif "service_account" in cfg:
            sa_dict = dict(cfg.get("service_account"))
        else:
            _last_drive_error = "Missing service account credentials in secrets."
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


# ------------------ SQLite helpers ------------------

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


# ------------------ Employees (Add / Edit / Delete by selection) ------------------

def ui_employees():
    st.subheader("Employees")
    tab_add, tab_edit = st.tabs(["➕ Add New", "✏️ Edit / 🗑 Delete"])

    # Add
    with tab_add:
        with st.form("add_emp"):
            c1, c2, c3 = st.columns(3)
            emp_id = c1.text_input("Emp ID *")
            name = c2.text_input("Name *")
            role = c3.text_input("Role")
            d1, d2, d3 = st.columns(3)
            salary_type = d1.selectbox("Salary Type *", ["Monthly", "PerDay"])
            monthly_salary = d2.number_input("Monthly Salary", min_value=0.0, step=100.0, format="%.2f")
            per_day_rate = d3.number_input("Per-Day Rate", min_value=0.0, step=10.0, format="%.2f")
            e1, e2, e3 = st.columns(3)
            doj = e1.date_input("Date of Joining", value=date.today())
            active = e2.checkbox("Active", value=True)
            bank = e3.text_input("Bank / UPI")

            if st.form_submit_button("Save Employee"):
                if not emp_id or not name:
                    st.error("Emp ID and Name are required")
                else:
                    execute("""
                        INSERT INTO employees(emp_id,name,role,salary_type,monthly_salary,per_day_rate,doj,active,bank)
                        VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(emp_id) DO UPDATE SET
                            name=excluded.name,
                            role=excluded.role,
                            salary_type=excluded.salary_type,
                            monthly_salary=excluded.monthly_salary,
                            per_day_rate=excluded.per_day_rate,
                            doj=excluded.doj,
                            active=excluded.active,
                            bank=excluded.bank
                    """, (
                        emp_id, name, role, salary_type,
                        monthly_salary if salary_type == "Monthly" else None,
                        per_day_rate if salary_type == "PerDay" else None,
                        doj.isoformat(), 1 if active else 0, bank
                    ))
                    st.success("Employee saved/updated.")

    # Edit / Delete
    with tab_edit:
        all_emps = df_from_query("""
            SELECT emp_id,name,role,salary_type,monthly_salary,per_day_rate,doj,active,bank
            FROM employees ORDER BY emp_id
        """)

        if all_emps.empty:
            st.info("No employees yet. Add one in the 'Add New' tab.")
        else:
            left, right = st.columns([1, 2])
            with left:
                st.caption("Pick an employee")
                options = [f"{r.emp_id} — {r.name}" for _, r in all_emps.iterrows()]
                mapping = {f"{r.emp_id} — {r.name}": r.emp_id for _, r in all_emps.iterrows()}
                chosen = st.selectbox("Employee", options)
                sel = all_emps[all_emps.emp_id == mapping[chosen]].iloc[0]

            with right:
                with st.form("edit_emp"):
                    c1, c2, c3 = st.columns(3)
                    emp_id_edit = c1.text_input("Emp ID *", value=sel.emp_id, disabled=True)
                    name_edit = c2.text_input("Name *", value=sel.name)
                    role_edit = c3.text_input("Role", value=sel.role)

                    d1, d2, d3 = st.columns(3)
                    salary_type_edit = d1.selectbox(
                        "Salary Type *", ["Monthly", "PerDay"],
                        index=0 if sel.salary_type == "Monthly" else 1
                    )
                    monthly_salary_edit = d2.number_input(
                        "Monthly Salary", min_value=0.0, step=100.0, format="%.2f",
                        value=float(sel.monthly_salary or 0)
                    )
                    per_day_rate_edit = d3.number_input(
                        "Per-Day Rate", min_value=0.0, step=10.0, format="%.2f",
                        value=float(sel.per_day_rate or 0)
                    )

                    e1, e2, e3 = st.columns(3)
                    existing_doj = pd.to_datetime(sel.doj).date() if pd.notna(sel.doj) and str(sel.doj) != "" else date.today()
                    doj_edit = e1.date_input("Date of Joining", value=existing_doj)
                    active_edit = e2.checkbox("Active", value=bool(sel.active))
                    bank_edit = e3.text_input("Bank / UPI", value=sel.bank if pd.notna(sel.bank) else "")

                    if st.form_submit_button("💾 Save Changes"):
                        execute("""
                            UPDATE employees
                               SET name=?, role=?, salary_type=?, monthly_salary=?, per_day_rate=?, doj=?, active=?, bank=?
                             WHERE emp_id=?
                        """, (
                            name_edit, role_edit, salary_type_edit,
                            monthly_salary_edit if salary_type_edit == "Monthly" else None,
                            per_day_rate_edit if salary_type_edit == "PerDay" else None,
                            doj_edit.isoformat(), 1 if active_edit else 0, bank_edit,
                            emp_id_edit
                        ))
                        st.success("Employee updated.")

                st.markdown("---")
                with st.expander("🗑 Delete this employee"):
                    st.warning("Deleting an employee does NOT automatically delete attendance/bonuses/deductions unless you choose so.")
                    also = st.checkbox("Also delete this employee's attendance, bonuses, and deductions")
                    if st.button(f"Delete {sel.emp_id} — {sel.name}"):
                        if also:
                            execute("DELETE FROM attendance WHERE emp_id=?", (sel.emp_id,))
                            execute("DELETE FROM bonuses WHERE emp_id=?", (sel.emp_id,))
                            execute("DELETE FROM deductions WHERE emp_id=?", (sel.emp_id,))
                        execute("DELETE FROM employees WHERE emp_id=?", (sel.emp_id,))
                        st.success(f"Deleted {sel.emp_id} — {sel.name}")

        st.markdown("### All employees")
        st.dataframe(all_emps, use_container_width=True)


# ------------------ Attendance (Single) ------------------

def ui_attendance():
    st.subheader("Attendance (Single Entry)")
    ids = df_from_query("SELECT emp_id FROM employees WHERE active=1 ORDER BY emp_id")
    emp_ids = [r["emp_id"] for _, r in ids.iterrows()]
    with st.form("add_att"):
        d, e, s, n = st.columns(4)
        day = d.date_input("Date", value=date.today())
        emp = e.selectbox("Emp ID", emp_ids)
        status = s.selectbox("Status", ["Present", "Absent", "Half Day", "Weekly Off"])
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


# ------------------ Attendance (Calendar — click to set) ------------------

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
        st.session_state[sess_key] = {d: existing_map.get(d) for d in range(1, days_in_month + 1)}
    calmap = st.session_state[sess_key]

    cycle = ["Present", "Absent", "Half Day", "Weekly Off", None]
    code = {"Present": "P", "Absent": "A", "Half Day": "H", "Weekly Off": "O", None: " "}

    def next_status(cur):
        try:
            i = cycle.index(cur)
        except ValueError:
            i = len(cycle) - 1
        return cycle[(i + 1) % len(cycle)]

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
                    calmap[d] = next_status(calmap.get(d))

    st.markdown("---")
    st.write("**Quick Set by Range** (e.g., 1–10 Present, 11 Absent, 12 Half Day, 13–end Present)")
    r1, r2, r3, r4, r5 = st.columns(5)
    days_in_month = calendar.monthrange(year, month)[1]
    d_start = int(r1.number_input("Start day", 1, days_in_month, 1))
    d_end = int(r2.number_input("End day", 1, days_in_month, days_in_month))
    rstatus = r3.selectbox("Status", ["Present", "Absent", "Half Day", "Weekly Off"])
    if r4.button("Apply Range"):
        a, b = min(d_start, d_end), max(d_start, d_end)
        for d in range(a, b + 1):
            calmap[d] = rstatus
        st.success(f"Applied {rstatus} to {a}–{b}.")
    if r5.button("Fill unset days as Present"):
        for d in calmap:
            if calmap[d] is None:
                calmap[d] = "Present"
        st.success("Unset days filled as Present.")

    csave1, csave2, csave3 = st.columns(3)
    if csave1.button("Clear month (not saved)"):
        for d in calmap:
            calmap[d] = None
        st.warning("Cleared this month's statuses (not saved).")

    if csave2.button("⤵ Restore now (pull from Drive)"):
        if drive_pull(DB_PATH):
            st.success("Restored DB from Drive (manual). Reloading calendar…")
        else:
            st.error(_last_drive_error or "Restore failed. Check secrets/sharing/file_id.")

    if csave3.button("💾 Save to database"):
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
                cur.execute(
                    "INSERT OR IGNORE INTO attendance(day,emp_id,status,note) VALUES(?,?,?,?)",
                    (dstr, emp_id, s, "calendar")
                )
            conn.commit()
        st.success("Saved calendar to database.")
        if GDRIVE_ENABLED and AUTO_BACKUP:
            drive_push(DB_PATH)


# ------------------ Bonuses (add + edit/delete + list) ------------------

def ui_bonuses():
    st.subheader("Bonuses")
    tabs = st.tabs(["➕ Add", "✏️ Edit / 🗑 Delete", "📜 List"])

    # Add
    with tabs[0]:
        ids = df_from_query("SELECT emp_id FROM employees WHERE active=1 ORDER BY emp_id")
        emp_ids = [r["emp_id"] for _, r in ids.iterrows()]
        with st.form("add_bonus"):
            c1, c2, c3 = st.columns(3)
            day = c1.date_input("Date", date.today())
            emp = c2.selectbox("Emp ID", emp_ids)
            amount = c3.number_input("Amount", min_value=0.0, step=100.0, format="%.2f")
            note = st.text_input("Note")
            if st.form_submit_button("Save Bonus"):
                execute("INSERT INTO bonuses(day,emp_id,amount,note) VALUES(?,?,?,?)",
                        (day.isoformat(), emp, amount, note))
                st.success("Saved.")

    # Edit/Delete
    with tabs[1]:
        df = df_from_query("SELECT id, day, emp_id, amount, note FROM bonuses ORDER BY day DESC, id DESC")
        if df.empty:
            st.info("No bonuses yet.")
        else:
            row_label = df.apply(lambda r: f"{r.id} — {r.day} — {r.emp_id} — {money(r.amount)}", axis=1)
            choose = st.selectbox("Pick an entry", list(row_label))
            row_id = int(choose.split(" — ")[0])
            row = df[df.id == row_id].iloc[0]

            with st.form("edit_bonus"):
                c1, c2, c3 = st.columns(3)
                day = c1.date_input("Date", pd.to_datetime(row.day).date())
                emp = c2.text_input("Emp ID", value=row.emp_id)
                amount = c3.number_input("Amount", min_value=0.0, step=100.0, format="%.2f",
                                         value=float(row.amount))
                note = st.text_input("Note", value=row.note if pd.notna(row.note) else "")
                save = st.form_submit_button("💾 Save changes")
            del_btn = st.button("🗑 Delete this entry")

            if save:
                execute("UPDATE bonuses SET day=?, emp_id=?, amount=?, note=? WHERE id=?",
                        (day.isoformat(), emp, amount, note, row_id))
                st.success("Updated.")
            if del_btn:
                execute("DELETE FROM bonuses WHERE id=?", (row_id,))
                st.success("Deleted.")

    # List
    with tabs[2]:
        st.dataframe(df_from_query(
            "SELECT id, day, emp_id, amount, note FROM bonuses ORDER BY day DESC, id DESC"
        ), use_container_width=True)


# ------------------ Deductions (add + edit/delete + list) ------------------

def ui_deductions():
    st.subheader("Deductions")
    tabs = st.tabs(["➕ Add", "✏️ Edit / 🗑 Delete", "📜 List"])

    # Add
    with tabs[0]:
        ids = df_from_query("SELECT emp_id FROM employees WHERE active=1 ORDER BY emp_id")
        emp_ids = [r["emp_id"] for _, r in ids.iterrows()]
        with st.form("add_ded"):
            c1, c2, c3 = st.columns(3)
            day = c1.date_input("Date", date.today())
            emp = c2.selectbox("Emp ID", emp_ids)
            dtype = c3.selectbox("Type", ["Advance", "Other"])
            amount = st.number_input("Amount", min_value=0.0, step=100.0, format="%.2f")
            note = st.text_input("Note")
            if st.form_submit_button("Save Deduction"):
                execute("INSERT INTO deductions(day,emp_id,dtype,amount,note) VALUES(?,?,?,?,?)",
                        (day.isoformat(), emp, dtype, amount, note))
                st.success("Saved.")

    # Edit/Delete
    with tabs[1]:
        df = df_from_query("SELECT id, day, emp_id, dtype, amount, note FROM deductions ORDER BY day DESC, id DESC")
        if df.empty:
            st.info("No deductions yet.")
        else:
            row_label = df.apply(lambda r: f"{r.id} — {r.day} — {r.emp_id} — {r.dtype} — {money(r.amount)}", axis=1)
            choose = st.selectbox("Pick an entry", list(row_label))
            row_id = int(choose.split(" — ")[0])
            row = df[df.id == row_id].iloc[0]

            with st.form("edit_ded"):
                c1, c2, c3, c4 = st.columns(4)
                day = c1.date_input("Date", pd.to_datetime(row.day).date())
                emp = c2.text_input("Emp ID", value=row.emp_id)
                dtype = c3.selectbox("Type", ["Advance", "Other"], index=0 if row.dtype == "Advance" else 1)
                amount = c4.number_input("Amount", min_value=0.0, step=100.0, format="%.2f",
                                         value=float(row.amount))
                note = st.text_input("Note", value=row.note if pd.notna(row.note) else "")
                save = st.form_submit_button("💾 Save changes")
            del_btn = st.button("🗑 Delete this entry")

            if save:
                execute("UPDATE deductions SET day=?, emp_id=?, dtype=?, amount=?, note=? WHERE id=?",
                        (day.isoformat(), emp, dtype, amount, note, row_id))
                st.success("Updated.")
            if del_btn:
                execute("DELETE FROM deductions WHERE id=?", (row_id,))
                st.success("Deleted.")

    # List
    with tabs[2]:
        st.dataframe(df_from_query(
            "SELECT id, day, emp_id, dtype, amount, note FROM deductions ORDER BY day DESC, id DESC"
        ), use_container_width=True)


# ------------------ Payroll & Payslip (with PDF exports) ------------------

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
    df[["present", "half_day", "absent", "bonus", "deduction"]] = df[
        ["present", "half_day", "absent", "bonus", "deduction"]
    ].fillna(0)

    rows = []
    for _, r in df.iterrows():
        if r["salary_type"] == "Monthly":
            base = float(r["monthly_salary"] or 0)
            leave = (base / LEAVE_DIVISOR) * (r["absent"] + 0.5 * r["half_day"])
        else:
            rate = float(r["per_day_rate"] or 0)
            base = (r["present"] + 0.5 * r["half_day"]) * rate
            leave = 0.0
        gross = base - leave + float(r["bonus"])
        net = gross - float(r["deduction"])
        rows.append([
            r["emp_id"], r["name"], r["salary_type"],
            int(r["present"]), int(r["half_day"]), int(r["absent"]),
            round(base, 2), round(leave, 2),
            round(float(r["bonus"]), 2), round(float(r["deduction"]), 2),
            round(gross, 2), round(net, 2)
        ])
    return pd.DataFrame(rows, columns=[
        "EmpID", "Name", "Type", "Present", "Half", "Absent",
        "Base", "LeaveDed", "Bonus", "Deduction", "Gross", "Net"
    ])


# --------- PDF generators ---------

def pdf_payslip(emp_label, erow, start_date, end_date, present, half_day, absent,
                base, leave, bonuses_df, deductions_df, net) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    x_margin = 20 * mm
    y = h - 20 * mm

    def line(txt, inc=7 * mm, bold=False):
        nonlocal y
        if bold:
            c.setFont("Helvetica-Bold", 11)
        else:
            c.setFont("Helvetica", 11)
        c.drawString(x_margin, y, txt)
        y -= inc

    c.setFont("Helvetica-Bold", 14)
    c.drawString(x_margin, y, "Payslip")
    y -= 10 * mm

    line(f"Employee: {emp_label}")
    line(f"Period: {start_date} to {end_date}")
    line(f"Salary Type: {erow.salary_type}")
    line("")

    line("Attendance", bold=True)
    line(f"Present: {present}   Half-day: {half_day}   Absent: {absent}")
    line("")

    line("Amounts", bold=True)
    line(f"Base Pay: {money(base)}")
    line(f"Leave Deduction: {money(leave)}")

    # Bonuses
    line("")
    line("Bonuses (itemized)", bold=True)
    if bonuses_df.empty:
        line("  None")
    else:
        for _, r in bonuses_df.iterrows():
            note = f" — {r['note']}" if pd.notna(r['note']) and r['note'] else ""
            line(f"  {r['day']}: {money(r['amount'])}{note}")

    # Deductions
    line("")
    line("Deductions (itemized)", bold=True)
    if deductions_df.empty:
        line("  None")
    else:
        for _, r in deductions_df.iterrows():
            note = f" — {r['note']}" if pd.notna(r['note']) and r['note'] else ""
            line(f"  {r['day']} [{r['dtype']}]: {money(r['amount'])}{note}")

    line("")
    line(f"Net Payable: {money(net)}", bold=True)

    c.showPage()
    c.save()
    return buf.getvalue()


def pdf_payroll(df, year, month) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    x_margin = 12 * mm
    y_start = h - 18 * mm
    row_h = 6.5 * mm

    def header():
        c.setFont("Helvetica-Bold", 13)
        c.drawString(x_margin, y_start + 6 * mm, f"Payroll Summary — {year}-{month:02d}")
        c.setFont("Helvetica-Bold", 9)
        y = y_start
        cols = ["EmpID", "Name", "Type", "P", "H", "A", "Base", "Leave", "Bonus", "Ded", "Gross", "Net"]
        x_pos = [x_margin, x_margin + 20*mm, x_margin + 70*mm, x_margin + 92*mm,
                 x_margin + 100*mm, x_margin + 108*mm, x_margin + 116*mm, x_margin + 136*mm,
                 x_margin + 156*mm, x_margin + 176*mm, x_margin + 196*mm, x_margin + 216*mm]
        for col, x in zip(cols, x_pos):
            c.drawString(x, y, col)
        return y - row_h, x_pos

    y, x_pos = header()
    c.setFont("Helvetica", 9)

    for _, r in df.iterrows():
        if y < 20 * mm:
            c.showPage()
            y, x_pos = header()
            c.setFont("Helvetica", 9)

        vals = [
            r["EmpID"], str(r["Name"])[:20], r["Type"], r["Present"], r["Half"], r["Absent"],
            money(r["Base"]), money(r["LeaveDed"]), money(r["Bonus"]), money(r["Deduction"]),
            money(r["Gross"]), money(r["Net"])
        ]
        for val, x in zip(vals, x_pos):
            c.drawString(x, y, str(val))
        y -= row_h

    c.setFont("Helvetica-Bold", 10)
    c.drawString(x_margin, y - 4 * mm, f"Total Net: {money(df['Net'].sum())}")

    c.showPage()
    c.save()
    return buf.getvalue()


def ui_payroll():
    st.subheader("Payroll")
    c1, c2, c3 = st.columns(3)
    year = int(c1.number_input("Year", 2000, 2100, date.today().year))
    month = int(c2.number_input("Month", 1, 12, date.today().month))
    if c3.button("Calculate"):
        df = payroll_df(year, month)
        st.dataframe(df, use_container_width=True)
        st.metric("Total Net", money(df["Net"].sum()))

        # CSV & Excel
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Download Payroll CSV", data=csv,
                           file_name=f"payroll_{year}_{month:02d}.csv", mime="text/csv")
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Payroll")
        st.download_button("Download Payroll Excel", data=output.getvalue(),
                           file_name=f"payroll_{year}_{month:02d}.xlsx")

        # PDF
        pdf_bytes = pdf_payroll(df, year, month)
        st.download_button("Download Payroll PDF", data=pdf_bytes,
                           file_name=f"payroll_{year}_{month:02d}.pdf", mime="application/pdf")


def ui_payslip():
    st.subheader("Payslip (Itemized)")

    emps = df_from_query("""
        SELECT emp_id, name, salary_type, monthly_salary, per_day_rate
        FROM employees WHERE active=1 ORDER BY emp_id
    """)
    if emps.empty:
        st.info("Add an employee first.")
        return

    emap = {f"{r['emp_id']} — {r['name']}": r['emp_id'] for _, r in emps.iterrows()}
    c1, c2, c3 = st.columns(3)
    emp_label = c1.selectbox("Employee", list(emap.keys()))
    start_date = c2.date_input("From", value=date(date.today().year, date.today().month, 1))
    end_date = c3.date_input("To", value=date.today())

    if st.button("Generate payslip"):
        emp_id = emap[emp_label]
        erow = emps[emps.emp_id == emp_id].iloc[0]

        att = df_from_query("""
            SELECT
              SUM(CASE WHEN status='Present'  THEN 1 ELSE 0 END) AS present,
              SUM(CASE WHEN status='Half Day' THEN 1 ELSE 0 END) AS half_day,
              SUM(CASE WHEN status='Absent'  THEN 1 ELSE 0 END) AS absent
            FROM attendance
            WHERE emp_id=? AND day>=? AND day<=?
        """, (emp_id, start_date.isoformat(), end_date.isoformat()))
        present = int(att.iloc[0]["present"] or 0)
        half_day = int(att.iloc[0]["half_day"] or 0)
        absent = int(att.iloc[0]["absent"] or 0)

        bon = df_from_query("""
            SELECT day, amount, note FROM bonuses
            WHERE emp_id=? AND day>=? AND day<=? ORDER BY day
        """, (emp_id, start_date.isoformat(), end_date.isoformat()))
        ded = df_from_query("""
            SELECT day, dtype, amount, note FROM deductions
            WHERE emp_id=? AND day>=? AND day<=? ORDER BY day
        """, (emp_id, start_date.isoformat(), end_date.isoformat()))

        sum_bonus = float(bon["amount"].sum()) if not bon.empty else 0.0
        sum_ded = float(ded["amount"].sum()) if not ded.empty else 0.0

        if erow.salary_type == "Monthly":
            base = float(erow.monthly_salary or 0)
            leave = (base / LEAVE_DIVISOR) * (absent + 0.5 * half_day)
        else:
            rate = float(erow.per_day_rate or 0)
            base = rate * (present + 0.5 * half_day)
            leave = 0.0

        gross = base - leave + sum_bonus
        net = gross - sum_ded

        st.markdown(f"### {emp_label}")
        st.caption(f"Period: **{start_date} → {end_date}**")
        st.write(f"Salary type: **{erow.salary_type}**")
        cA, cB, cC, cD = st.columns(4)
        cA.metric("Present", present)
        cB.metric("Half Day", half_day)
        cC.metric("Absent", absent)
        cD.metric("Base Pay", money(base))
        cE, cF, cG = st.columns(3)
        cE.metric("Leave Deduction", money(leave))
        cF.metric("Total Bonuses", money(sum_bonus))
        cG.metric("Total Deductions", money(sum_ded))
        st.subheader(f"**Net Payable: {money(net)}**")

        st.markdown("#### Bonuses (itemized)")
        if bon.empty:
            st.info("No bonuses in this range.")
        else:
            st.dataframe(bon.rename(columns={"day": "Date", "amount": "Amount", "note": "Note"}),
                         use_container_width=True)

        st.markdown("#### Deductions (itemized)")
        if ded.empty:
            st.info("No deductions in this range.")
        else:
            st.dataframe(ded.rename(columns={"day": "Date", "dtype": "Type", "amount": "Amount", "note": "Note"}),
                         use_container_width=True)

        # CSV summary
        slip = pd.DataFrame([{
            "EmpID": emp_id, "Name": emp_label.split(" — ", 1)[1],
            "From": start_date, "To": end_date,
            "Type": erow.salary_type, "Present": present, "Half Day": half_day, "Absent": absent,
            "Base": round(base, 2), "LeaveDeduction": round(leave, 2),
            "Bonuses": round(sum_bonus, 2), "Deductions": round(sum_ded, 2),
            "Net": round(net, 2)
        }])
        csv = slip.to_csv(index=False).encode("utf-8")
        st.download_button("Download Payslip Summary (CSV)", data=csv,
                           file_name=f"payslip_{emp_id}_{start_date}_{end_date}.csv", mime="text/csv")

        # PDF itemized
        pdf_bytes = pdf_payslip(emp_label, erow, start_date, end_date, present, half_day, absent,
                                base, leave, bon, ded, net)
        st.download_button("Download Payslip PDF", data=pdf_bytes,
                           file_name=f"payslip_{emp_id}_{start_date}_{end_date}.pdf", mime="application/pdf")


# ------------------ Main ------------------

def main():
    st.set_page_config(page_title="Payroll App (Drive Backup + PDF)", layout="wide")

    init_ok = _drive_init()
    with st.sidebar.expander("Cloud Sync (Google Drive)"):
        try:
            gdr = st.secrets.get("gdrive", {})
            st.caption(f"DEBUG → gdrive keys: {list(gdr.keys())}")
            st.caption(f"DEBUG → has service_account table: {'service_account' in gdr}")
            st.caption(f"DEBUG → has file_id: {bool(gdr.get('file_id'))}")
        except Exception as _e:
            st.caption(f"DEBUG → cannot read secrets: {_e}")

        if init_ok:
            if drive_pull(DB_PATH):
                st.success("Restored DB from Drive (startup)")
            else:
                st.info("Using local DB (no Drive restore)")
                if _last_drive_error:
                    st.caption(_last_drive_error)

            c1, c2, c3 = st.columns(3)
            if c1.button("⤴ Backup now"):
                if drive_push(DB_PATH):
                    st.success("Backed up to Drive")
                else:
                    st.error(_last_drive_error or "Backup failed")

            if c2.button("⤵ Restore now"):
                if drive_pull(DB_PATH):
                    st.success("Restored DB from Drive (manual)")
                else:
                    st.error(_last_drive_error or "Restore failed")

            if os.path.exists(DB_PATH):
                with open(DB_PATH, "rb") as f:
                    st.download_button("Download DB (payroll.db)", f, file_name="payroll.db")

            st.caption(f"AUTO_BACKUP: {'ON' if AUTO_BACKUP else 'OFF'} (set in secrets)")
        else:
            st.error("Drive init failed — using local DB only.")
            if _last_drive_error:
                st.caption(f"Reason: {_last_drive_error}")
            st.caption("Check secrets format and that the Drive file is shared with your service account (Editor).")

    init_db()

    st.title("💼 Payroll App (SQLite + Google Drive + PDF)")
    st.caption("Employees • Attendance • Calendar • Bonuses • Deductions • Payroll • Payslip • Drive backup")

    section = st.sidebar.radio(
        "Go to",
        ["Employees", "Attendance", "Attendance (Calendar)", "Bonuses", "Deductions", "Payroll", "Payslip"]
    )
    if section == "Employees":
        ui_employees()
    elif section == "Attendance":
        ui_attendance()
    elif section == "Attendance (Calendar)":
        ui_attendance_calendar()
    elif section == "Bonuses":
        ui_bonuses()
    elif section == "Deductions":
        ui_deductions()
    elif section == "Payroll":
        ui_payroll()
    elif section == "Payslip":
        ui_payslip()


if __name__ == "__main__":
    main()
