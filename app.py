# app.py — Payroll (Streamlit + SQLite) with Google Drive Restore/Backup
# ---------------------------------------------------------------------
# Features:
# - Employees (Add, Edit, Delete)
# - Attendance (Single), Attendance (Calendar click-to-set)
# - Bonuses, Deductions, Payroll, Payslip
# - Google Drive "restore point": pull payroll.db on startup, push on demand (or auto if enabled)
#
# Deploy tips:
# - Add your Drive secrets in .streamlit/secrets.toml (see template)
# - On Streamlit Cloud, set the same secrets in the app settings

import streamlit as st
import sqlite3
import pandas as pd
import io
import calendar
import json, time
from datetime import date

# ---- Google Drive sync (via pydrive2) ----
from pydrive2.auth import ServiceAccountCredentials
from pydrive2.drive import GoogleDrive

DB_PATH = "payroll.db"
LEAVE_DIVISOR = 30

# ------------------ Google Drive helpers ------------------

GDRIVE_ENABLED = False
GDRIVE_FILE_ID = None
AUTO_BACKUP = False
_drive = None
_last_push_ts = 0
PUSH_COOLDOWN_SEC = 2

def _drive_init():
    """Initialize Google Drive client from Streamlit secrets."""
    global GDRIVE_ENABLED, GDRIVE_FILE_ID, _drive, AUTO_BACKUP
    try:
        cfg = st.secrets.get("gdrive", None)
        if not cfg:
            return
        GDRIVE_FILE_ID = cfg.get("file_id")
        sa_json_str = cfg.get("service_account_json")
        AUTO_BACKUP = bool(cfg.get("auto_backup", False))
        if not GDRIVE_FILE_ID or not sa_json_str:
            return
        creds_dict = json.loads(sa_json_str)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        _drive = GoogleDrive(creds)
        GDRIVE_ENABLED = True
    except Exception as e:
        st.sidebar.warning(f"Drive init failed: {e}")
        GDRIVE_ENABLED = False

def drive_pull(local_path=DB_PATH):
    """Download payroll.db from Drive into local file."""
    if not GDRIVE_ENABLED: return False
    try:
        f = _drive.CreateFile({"id": GDRIVE_FILE_ID})
        f.GetContentFile(local_path)
        return True
    except Exception as e:
        st.sidebar.warning(f"Drive pull failed: {e}")
        return False

def drive_push(local_path=DB_PATH):
    """Upload local payroll.db to Drive (with small cooldown)."""
    global _last_push_ts
    if not GDRIVE_ENABLED: return False
    if time.time() - _last_push_ts < PUSH_COOLDOWN_SEC:
        return False
    try:
        f = _drive.CreateFile({"id": GDRIVE_FILE_ID})
        f.SetContentFile(local_path)
        f.Upload()
        _last_push_ts = time.time()
        return True
    except Exception as e:
        st.sidebar.warning(f"Drive push failed: {e}")
        return False

# ------------------ DB Helpers ------------------

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with get_conn() as conn:
        cur = conn.cursor()
        # Employees
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
        # Attendance
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
        # Deductions
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
        # Bonuses
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

# ------------------ Employees (Add / Edit / Delete) ------------------

def ui_employees():
    st.subheader("Employees")

    tab_add, tab_edit = st.tabs(["➕ Add New", "✏️ Edit / 🗑 Delete"])

    with tab_add:
        with st.form("add_emp"):
            c1, c2, c3 = st.columns(3)
            emp_id = c1.text_input("Emp ID *")
            name = c2.text_input("Name *")
            role = c3.text_input("Role")
            d1, d2, d3 = st.columns(3)
            salary_type = d1.selectbox("Salary Type *", ["Monthly","PerDay"])
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
                    """,(
                        emp_id, name, role, salary_type,
                        monthly_salary if salary_type=="Monthly" else None,
                        per_day_rate if salary_type=="PerDay" else None,
                        doj.isoformat(), 1 if active else 0, bank
                    ))
                    st.success("Employee saved/updated.")

    with tab_edit:
        all_emps = df_from_query(
            "SELECT emp_id,name,role,salary_type,monthly_salary,per_day_rate,doj,active,bank FROM employees ORDER BY emp_id"
        )

        if all_emps.empty:
            st.info("No employees yet. Add an employee in the 'Add New' tab.")
        else:
            left, right = st.columns([1,2])

            with left:
                st.caption("Select an employee to edit/delete")
                emp_choices = [f"{r.emp_id} — {r.name}" for _, r in all_emps.iterrows()]
                emp_map = {f"{r.emp_id} — {r.name}": r.emp_id for _, r in all_emps.iterrows()}
                pick = st.selectbox("Employee", emp_choices)
                sel_emp = all_emps[all_emps.emp_id == emp_map[pick]].iloc[0]

            with right:
                # --- Edit form ---
                with st.form("edit_emp"):
                    c1, c2, c3 = st.columns(3)
                    emp_id_edit = c1.text_input("Emp ID *", value=sel_emp.emp_id, disabled=True)
                    name_edit = c2.text_input("Name *", value=sel_emp.name)
                    role_edit = c3.text_input("Role", value=sel_emp.role)

                    d1, d2, d3 = st.columns(3)
                    salary_type_edit = d1.selectbox("Salary Type *", ["Monthly","PerDay"],
                                                    index=0 if sel_emp.salary_type=="Monthly" else 1)
                    monthly_salary_edit = d2.number_input("Monthly Salary", min_value=0.0, step=100.0, format="%.2f",
                                                          value=float(sel_emp.monthly_salary or 0))
                    per_day_rate_edit = d3.number_input("Per-Day Rate", min_value=0.0, step=10.0, format="%.2f",
                                                        value=float(sel_emp.per_day_rate or 0))

                    e1, e2, e3 = st.columns(3)
                    existing_doj = pd.to_datetime(sel_emp.doj).date() if pd.notna(sel_emp.doj) and str(sel_emp.doj)!="" else date.today()
                    doj_edit = e1.date_input("Date of Joining", value=existing_doj)
                    active_edit = e2.checkbox("Active", value=bool(sel_emp.active))
                    bank_edit = e3.text_input("Bank / UPI", value=sel_emp.bank if pd.notna(sel_emp.bank) else "")

                    if st.form_submit_button("💾 Save Changes"):
                        execute("""
                            UPDATE employees
                            SET name=?, role=?, salary_type=?, monthly_salary=?, per_day_rate=?, doj=?, active=?, bank=?
                            WHERE emp_id=?
                        """,(
                            name_edit, role_edit, salary_type_edit,
                            monthly_salary_edit if salary_type_edit=="Monthly" else None,
                            per_day_rate_edit if salary_type_edit=="PerDay" else None,
                            doj_edit.isoformat(), 1 if active_edit else 0, bank_edit,
                            emp_id_edit
                        ))
                        st.success("Employee updated.")

                st.markdown("---")
                with st.expander("🗑 Delete this employee"):
                    st.warning("Deleting an employee does NOT automatically delete related attendance/bonuses/deductions unless you choose so below.")
                    also_delete = st.checkbox("Also delete this employee's attendance, bonuses, and deductions")
                    st.caption(f"Emp ID to confirm: **{emp_id_edit}**")
                    confirm = st.text_input("Type the Emp ID to confirm delete")
                    if st.button("Delete Employee"):
                        if confirm != emp_id_edit:
                            st.error("Confirmation text does not match Emp ID.")
                        else:
                            if also_delete:
                                execute("DELETE FROM attendance WHERE emp_id=?", (emp_id_edit,))
                                execute("DELETE FROM bonuses WHERE emp_id=?", (emp_id_edit,))
                                execute("DELETE FROM deductions WHERE emp_id=?", (emp_id_edit,))
                            execute("DELETE FROM employees WHERE emp_id=?", (emp_id_edit,))
                            st.success(f"Deleted employee {emp_id_edit}.")

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
        st.session_state[sess_key] = {d: existing_map.get(d) for d in range(1, days_in_month+1)}
    calmap = st.session_state[sess_key]

    cycle = ["Present", "Absent", "Half Day", "Weekly Off", None]
    code = {"Present":"P","Absent":"A","Half Day":"H","Weekly Off":"O", None:" "}

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
    rstatus = r3.selectbox("Status", ["Present","Absent","Half Day","Weekly Off"])
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

    csave1, _, csave3 = st.columns(3)
    if csave1.button("Clear month (not saved)"):
        for d in calmap:
            calmap[d] = None
        st.warning("Cleared this month's statuses (not saved).")

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

# ------------------ Bonuses ------------------

def ui_bonuses():
    st.subheader("Bonuses")
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

    st.dataframe(df_from_query(
        "SELECT day, emp_id, amount, note FROM bonuses ORDER BY day DESC, emp_id"
    ), use_container_width=True)

# ------------------ Deductions ------------------

def ui_deductions():
    st.subheader("Deductions")
    ids = df_from_query("SELECT emp_id FROM employees WHERE active=1 ORDER BY emp_id")
    emp_ids = [r["emp_id"] for _, r in ids.iterrows()]
    with st.form("add_ded"):
        c1, c2, c3 = st.columns(3)
        day = c1.date_input("Date", date.today())
        emp = c2.selectbox("Emp ID", emp_ids)
        dtype = c3.selectbox("Type", ["Advance","Other"])
        amount = st.number_input("Amount", min_value=0.0, step=100.0, format="%.2f")
        note = st.text_input("Note")
        if st.form_submit_button("Save Deduction"):
            execute("INSERT INTO deductions(day,emp_id,dtype,amount,note) VALUES(?,?,?,?,?)",
                    (day.isoformat(), emp, dtype, amount, note))
            st.success("Saved.")

    st.dataframe(df_from_query(
        "SELECT day, emp_id, dtype, amount, note FROM deductions ORDER BY day DESC, emp_id"
    ), use_container_width=True)

# ------------------ Payroll & Payslip ------------------

def payroll_df(year, month):
    start, end = month_bounds(year, month)
    emps = df_from_query("SELECT emp_id,name,role,salary_type,monthly_salary,per_day_rate FROM employees WHERE active=1")
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
    df[["present","half_day","absent","bonus","deduction"]] = df[["present","half_day","absent","bonus","deduction"]].fillna(0)

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
            round(base,2), round(leave,2), round(float(r["bonus"]),2), round(float(r["deduction"]),2),
            round(gross,2), round(net,2)
        ])
    return pd.DataFrame(rows, columns=[
        "EmpID","Name","Type","Present","Half","Absent","Base","LeaveDed","Bonus","Deduction","Gross","Net"
    ])

def ui_payroll():
    st.subheader("Payroll")
    c1, c2, c3 = st.columns(3)
    year = int(c1.number_input("Year", 2000, 2100, date.today().year))
    month = int(c2.number_input("Month", 1, 12, date.today().month))
    if c3.button("Calculate"):
        df = payroll_df(year, month)
        st.dataframe(df, use_container_width=True)
        st.metric("Total Net", f"₹{df['Net'].sum():,.2f}")
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("Download Payroll CSV", data=csv, file_name=f"payroll_{year}_{month:02d}.csv", mime="text/csv")
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Payroll")
        st.download_button("Download Payroll Excel", data=output.getvalue(), file_name=f"payroll_{year}_{month:02d}.xlsx")

def ui_payslip():
    st.subheader("Payslip")
    emps = df_from_query("SELECT emp_id, name FROM employees WHERE active=1 ORDER BY emp_id")
    if emps.empty:
        st.info("Add an employee first.")
        return
    emp_map = {f"{r['emp_id']} — {r['name']}": r['emp_id'] for _, r in emps.iterrows()}
    c1, c2, c3, c4 = st.columns(4)
    emp_label = c1.selectbox("Employee", list(emp_map.keys()))
    year = int(c2.number_input("Year", 2000, 2100, date.today().year))
    month = int(c3.number_input("Month", 1, 12, date.today().month))
    if c4.button("Generate"):
        emp_id = emp_map[emp_label]
        df = payroll_df(year, month)
        row = df[df.EmpID == emp_id]
        if row.empty:
            st.warning("No payroll data for this employee/month.")
            return
        r = row.iloc[0]
        st.write(f"**Employee:** {r['Name']} ({r['EmpID']})")
        st.write(f"**Salary Type:** {r['Type']}")
        st.write(f"**Month:** {year}-{month:02d}")
        st.write("---")
        st.write(f"Present: {r['Present']} | Half-days: {r['Half']} | Absent: {r['Absent']}")
        st.write(f"Base Pay: ₹{r['Base']:,.2f}")
        st.write(f"Leave Deduction: ₹{r['LeaveDed']:,.2f}")
        st.write(f"Bonuses: ₹{r['Bonus']:,.2f}")
        st.write(f"Deductions: ₹{r['Deduction']:,.2f}")
        st.write(f"**Net Pay: ₹{r['Net']:,.2f}**")
        slip = r.to_frame().T
        csv = slip.to_csv(index=False).encode("utf-8")
        st.download_button("Download Payslip CSV", data=csv, file_name=f"payslip_{emp_id}_{year}_{month:02d}.csv", mime="text/csv")

# ------------------ Main ------------------

def main():
    st.set_page_config(page_title="Payroll App (Drive Backup)", layout="wide")

    # Drive restore/backup controls
    _drive_init()
    with st.sidebar.expander("Cloud Sync (Google Drive)"):
        if GDRIVE_ENABLED:
            pulled = drive_pull(DB_PATH)
            if pulled:
                st.success("Restored DB from Drive")
            else:
                st.info("Using local DB (no Drive restore)")
            if st.button("⤴ Backup now"):
                if drive_push(DB_PATH):
                    st.success("Backed up to Drive")
                else:
                    st.warning("Backup failed")
            st.caption(f"AUTO_BACKUP: {'ON' if AUTO_BACKUP else 'OFF'} (configure in secrets)")
        else:
            st.caption("Drive not configured. Add gdrive secrets to enable restore/backup.")

    init_db()
    st.title("💼 Payroll App (SQLite + Google Drive)")
    st.caption("Employees • Attendance • Calendar • Bonuses • Deductions • Payroll • Payslip • Drive backup")

    section = st.sidebar.radio(
        "Go to",
        ["Employees","Attendance","Attendance (Calendar)","Bonuses","Deductions","Payroll","Payslip"]
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
