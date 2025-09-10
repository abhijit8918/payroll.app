import streamlit as st
import sqlite3
import pandas as pd
from datetime import date
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials

DB_PATH = "payroll.db"
LEAVE_DIVISOR = 30

# =============== Helper rerun =====================
def _rerun():
    """Compatibility rerun for Streamlit"""
    try:
        st.rerun()
    except AttributeError:
        st.experimental_rerun()

# =============== DB Helpers =======================
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def execute(query, params=()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    conn.close()

def df_from_query(query, params=()):
    conn = get_conn()
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df

def init_db():
    execute("""
    CREATE TABLE IF NOT EXISTS employees(
        emp_id TEXT PRIMARY KEY,
        name TEXT, role TEXT,
        salary_type TEXT,
        monthly_salary REAL,
        per_day_rate REAL,
        doj TEXT,
        active INTEGER,
        bank TEXT
    )
    """)
    execute("""
    CREATE TABLE IF NOT EXISTS attendance(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_id TEXT, day TEXT, status TEXT
    )
    """)
    execute("""
    CREATE TABLE IF NOT EXISTS bonuses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_id TEXT, day TEXT, amount REAL, note TEXT
    )
    """)
    execute("""
    CREATE TABLE IF NOT EXISTS deductions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_id TEXT, day TEXT, dtype TEXT, amount REAL, note TEXT
    )
    """)

# =============== Google Drive Sync ================
GDRIVE_ENABLED = False
AUTO_BACKUP = False
_drive = None
_file_id = None

def _drive_init():
    global GDRIVE_ENABLED, AUTO_BACKUP, _drive, _file_id
    try:
        gdr = st.secrets.get("gdrive", {})
        if not gdr or "service_account" not in gdr or "file_id" not in gdr:
            return
        sa_dict = dict(gdr["service_account"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(
            sa_dict, scopes=["https://www.googleapis.com/auth/drive"]
        )
        gauth = GoogleAuth()
        gauth.credentials = creds
        _drive = GoogleDrive(gauth)
        _file_id = gdr["file_id"]
        AUTO_BACKUP = gdr.get("auto_backup", False)
        GDRIVE_ENABLED = True
    except Exception as e:
        st.sidebar.error(f"Drive init failed: {e}")

def drive_pull(local_path):
    try:
        f = _drive.CreateFile({"id": _file_id})
        f.GetContentFile(local_path)
        return True
    except Exception as e:
        st.sidebar.error(f"Drive pull failed: {e}")
        return False

def drive_push(local_path):
    try:
        f = _drive.CreateFile({"id": _file_id})
        f.SetContentFile(local_path)
        f.Upload()
        return True
    except Exception as e:
        st.sidebar.error(f"Drive push failed: {e}")
        return False

# =============== UI Components ====================
def render_edit_delete_buttons(label, edit_key, delete_key):
    c1, c2 = st.columns([1, 1])
    edit = c1.button("✏️ Edit", key=edit_key)
    delete = c2.button("🗑 Delete", key=delete_key)
    return edit, delete

# =============== Employees ========================
def ui_employees():
    st.subheader("Employees")

    with st.form("add_emp"):
        c1, c2, c3 = st.columns(3)
        emp_id = c1.text_input("Emp ID *")
        name = c2.text_input("Name *")
        role = c3.text_input("Role")
        d1, d2, d3 = st.columns(3)
        salary_type = d1.selectbox("Salary Type *", ["Monthly","PerDay"])
        monthly_salary = d2.number_input("Monthly Salary", min_value=0.0, step=100.0)
        per_day_rate = d3.number_input("Per-Day Rate", min_value=0.0, step=10.0)
        e1, e2, e3 = st.columns(3)
        doj = e1.date_input("Date of Joining", value=date.today())
        active = e2.checkbox("Active", value=True)
        bank = e3.text_input("Bank / UPI")

        if st.form_submit_button("Save Employee"):
            if not emp_id or not name:
                st.error("Emp ID and Name required")
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
                      per_day_rate if salary_type=="PerDay" else None,
                      doj.isoformat(), 1 if active else 0, bank))
                st.success("Employee saved/updated.")
                _rerun()

    df = df_from_query("SELECT * FROM employees ORDER BY emp_id")
    if df.empty:
        st.info("No employees.")
        return

    for _, row in df.iterrows():
        with st.expander(f"{row.emp_id} — {row.name}"):
            st.write(row.to_dict())
            edit, delete = render_edit_delete_buttons(
                f"{row.emp_id}", f"edit_{row.emp_id}", f"del_{row.emp_id}"
            )
            if edit:
                st.session_state[f"editing_emp_{row.emp_id}"] = row.to_dict()
            if delete:
                st.session_state[f"deleting_emp_{row.emp_id}"] = row.to_dict()
                _rerun()

    # Handle edits
    for k in [k for k in st.session_state if k.startswith("editing_emp_")]:
        data = st.session_state[k]
        with st.form(f"editform_{data['emp_id']}"):
            name = st.text_input("Name", value=data["name"])
            role = st.text_input("Role", value=data["role"])
            salary_type = st.selectbox("Salary Type", ["Monthly","PerDay"], index=0 if data["salary_type"]=="Monthly" else 1)
            monthly_salary = st.number_input("Monthly Salary", value=float(data["monthly_salary"] or 0))
            per_day_rate = st.number_input("Per-Day Rate", value=float(data["per_day_rate"] or 0))
            bank = st.text_input("Bank/UPI", value=data["bank"] or "")
            save = st.form_submit_button("💾 Save")
        if save:
            execute("""
            UPDATE employees SET name=?,role=?,salary_type=?,monthly_salary=?,per_day_rate=?,bank=? WHERE emp_id=?
            """, (name, role, salary_type,
                  monthly_salary if salary_type=="Monthly" else None,
                  per_day_rate if salary_type=="PerDay" else None,
                  bank, data["emp_id"]))
            st.success("Updated.")
            del st.session_state[k]
            _rerun()

    # Handle deletes
    for k in [k for k in st.session_state if k.startswith("deleting_emp_")]:
        data = st.session_state[k]
        st.error(f"Confirm delete employee {data['emp_id']} — {data['name']}?")
        c1, c2 = st.columns(2)
        if c1.button("✅ Confirm", key=f"c_{data['emp_id']}"):
            execute("DELETE FROM attendance WHERE emp_id=?", (data["emp_id"],))
            execute("DELETE FROM bonuses WHERE emp_id=?", (data["emp_id"],))
            execute("DELETE FROM deductions WHERE emp_id=?", (data["emp_id"],))
            execute("DELETE FROM employees WHERE emp_id=?", (data["emp_id"],))
            st.success("Deleted")
            del st.session_state[k]
            _rerun()
        if c2.button("❌ Cancel", key=f"x_{data['emp_id']}"):
            del st.session_state[k]
            _rerun()

# =============== Bonuses ==========================
def ui_bonuses():
    st.subheader("Bonuses")
    with st.form("add_bonus"):
        ids = df_from_query("SELECT emp_id FROM employees WHERE active=1 ORDER BY emp_id")
        emp_ids = [r["emp_id"] for _, r in ids.iterrows()]
        day = st.date_input("Date", date.today())
        emp = st.selectbox("Emp ID", emp_ids)
        amount = st.number_input("Amount", min_value=0.0, step=100.0)
        note = st.text_input("Note")
        if st.form_submit_button("Save Bonus"):
            execute("INSERT INTO bonuses(day,emp_id,amount,note) VALUES(?,?,?,?)",
                    (day.isoformat(), emp, amount, note))
            st.success("Saved")
            _rerun()

    df = df_from_query("SELECT * FROM bonuses ORDER BY day DESC")
    if df.empty:
        st.info("No bonuses.")
        return
    for _, row in df.iterrows():
        with st.expander(f"Bonus {row.id} — {row.emp_id} — ₹{row.amount}"):
            st.write(row.to_dict())
            edit, delete = render_edit_delete_buttons(
                f"b{row.id}", f"edit_b{row.id}", f"del_b{row.id}"
            )
            if edit:
                st.session_state[f"editing_bonus_{row.id}"] = row.to_dict()
            if delete:
                st.session_state[f"deleting_bonus_{row.id}"] = row.to_dict()
                _rerun()

    for k in [k for k in st.session_state if k.startswith("editing_bonus_")]:
        data = st.session_state[k]
        with st.form(f"editb_{data['id']}"):
            amount = st.number_input("Amount", value=float(data["amount"]))
            note = st.text_input("Note", value=data["note"] or "")
            save = st.form_submit_button("💾 Save")
        if save:
            execute("UPDATE bonuses SET amount=?, note=? WHERE id=?", (amount, note, data["id"]))
            st.success("Updated")
            del st.session_state[k]
            _rerun()

    for k in [k for k in st.session_state if k.startswith("deleting_bonus_")]:
        data = st.session_state[k]
        st.error(f"Confirm delete bonus {data['id']} for {data['emp_id']} amount ₹{data['amount']}?")
        c1,c2=st.columns(2)
        if c1.button("✅ Confirm", key=f"c_b{data['id']}"):
            execute("DELETE FROM bonuses WHERE id=?", (data["id"],))
            del st.session_state[k]
            _rerun()
        if c2.button("❌ Cancel", key=f"x_b{data['id']}"):
            del st.session_state[k]
            _rerun()

# =============== Deductions =======================
def ui_deductions():
    st.subheader("Deductions")
    with st.form("add_ded"):
        ids = df_from_query("SELECT emp_id FROM employees WHERE active=1 ORDER BY emp_id")
        emp_ids = [r["emp_id"] for _, r in ids.iterrows()]
        day = st.date_input("Date", date.today())
        emp = st.selectbox("Emp ID", emp_ids)
        dtype = st.selectbox("Type", ["Advance","Other"])
        amount = st.number_input("Amount", min_value=0.0, step=100.0)
        note = st.text_input("Note")
        if st.form_submit_button("Save Deduction"):
            execute("INSERT INTO deductions(day,emp_id,dtype,amount,note) VALUES(?,?,?,?,?)",
                    (day.isoformat(), emp, dtype, amount, note))
            st.success("Saved")
            _rerun()

    df = df_from_query("SELECT * FROM deductions ORDER BY day DESC")
    if df.empty:
        st.info("No deductions.")
        return
    for _, row in df.iterrows():
        with st.expander(f"Deduction {row.id} — {row.emp_id} — ₹{row.amount}"):
            st.write(row.to_dict())
            edit, delete = render_edit_delete_buttons(
                f"d{row.id}", f"edit_d{row.id}", f"del_d{row.id}"
            )
            if edit:
                st.session_state[f"editing_ded_{row.id}"] = row.to_dict()
            if delete:
                st.session_state[f"deleting_ded_{row.id}"] = row.to_dict()
                _rerun()

    for k in [k for k in st.session_state if k.startswith("editing_ded_")]:
        data = st.session_state[k]
        with st.form(f"editd_{data['id']}"):
            amount = st.number_input("Amount", value=float(data["amount"]))
            note = st.text_input("Note", value=data["note"] or "")
            save = st.form_submit_button("💾 Save")
        if save:
            execute("UPDATE deductions SET amount=?, note=? WHERE id=?", (amount, note, data["id"]))
            st.success("Updated")
            del st.session_state[k]
            _rerun()

    for k in [k for k in st.session_state if k.startswith("deleting_ded_")]:
        data = st.session_state[k]
        st.error(f"Confirm delete deduction {data['id']} for {data['emp_id']} amount ₹{data['amount']}?")
        c1,c2=st.columns(2)
        if c1.button("✅ Confirm", key=f"c_d{data['id']}"):
            execute("DELETE FROM deductions WHERE id=?", (data["id"],))
            del st.session_state[k]
            _rerun()
        if c2.button("❌ Cancel", key=f"x_d{data['id']}"):
            del st.session_state[k]
            _rerun()

# =============== Payslip ==========================
def ui_payslip():
    st.subheader("Payslip (Itemized)")
    emps = df_from_query("SELECT emp_id, name, salary_type, monthly_salary, per_day_rate FROM employees WHERE active=1")
    if emps.empty:
        st.info("No employees.")
        return
    emap = {f"{r['emp_id']} — {r['name']}": r['emp_id'] for _, r in emps.iterrows()}
    c1,c2,c3=st.columns(3)
    emp_label = c1.selectbox("Employee", list(emap.keys()))
    start_date = c2.date_input("From", value=date(date.today().year, date.today().month, 1))
    end_date = c3.date_input("To", value=date.today())

    if st.button("Generate payslip"):
        emp_id = emap[emp_label]
        erow = emps[emps.emp_id==emp_id].iloc[0]

        bon = df_from_query("SELECT day,amount,note FROM bonuses WHERE emp_id=? AND day>=? AND day<=?",
                            (emp_id,start_date.isoformat(),end_date.isoformat()))
        ded = df_from_query("SELECT day,dtype,amount,note FROM deductions WHERE emp_id=? AND day>=? AND day<=?",
                            (emp_id,start_date.isoformat(),end_date.isoformat()))
        sum_bonus = float(bon["amount"].sum()) if not bon.empty else 0.0
        sum_ded = float(ded["amount"].sum()) if not ded.empty else 0.0
        base = float(erow.monthly_salary or 0) if erow.salary_type=="Monthly" else float(erow.per_day_rate or 0)
        net = base + sum_bonus - sum_ded

        st.write(f"Net Payable: ₹{net}")
        st.dataframe(bon)
        st.dataframe(ded)

# =============== Main =============================
def main():
    st.set_page_config(page_title="Payroll App", layout="wide")
    _drive_init()
    with st.sidebar.expander("Cloud Sync (Google Drive)"):
        if GDRIVE_ENABLED:
            if drive_pull(DB_PATH):
                st.success("Restored DB from Drive (startup)")
            if st.button("Backup now"): drive_push(DB_PATH)
            if st.button("Restore now"): drive_pull(DB_PATH); _rerun()
        else:
            st.caption("Drive not configured")

    init_db()
    st.title("💼 Payroll App")

    section = st.sidebar.radio("Go to",["Employees","Bonuses","Deductions","Payslip"])
    if section=="Employees": ui_employees()
    elif section=="Bonuses": ui_bonuses()
    elif section=="Deductions": ui_deductions()
    elif section=="Payslip": ui_payslip()

if __name__=="__main__":
    main()
