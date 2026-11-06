import os, sqlite3, secrets, logging, requests, hashlib, base64, time
from datetime import datetime
from flask import Flask, request, redirect, url_for, render_template_string, flash, jsonify, session, send_from_directory

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "paypal_escrow.db")
app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox")
PAYPAL_BASE = "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"

# --- CURRENCY ---
RATE_CACHE = {"time": 0, "rates": {}}
def get_rates():
    now = time.time()
    if RATE_CACHE["rates"] and (now - RATE_CACHE["time"] < 3600):
        return RATE_CACHE["rates"]
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        RATE_CACHE["rates"] = r.json().get("rates", {})
        RATE_CACHE["time"] = now
        return RATE_CACHE["rates"]
    except:
        return {"USD":1,"KES":129.91,"EUR":0.92,"GBP":0.79,"NGN":1580,"UGX":3720,"TZS":2620,"ZAR":18.2,"INR":83.5,"CAD":1.36,"AUD":1.52}
def convert(amount, frm, to):
    frm, to = frm.upper(), to.upper()
    if frm == to: return float(amount)
    rates = get_rates()
    usd = float(amount) / rates.get(frm, 1) if frm != "USD" else float(amount)
    return round(usd, 2) if to == "USD" else round(usd * rates.get(to, 1), 2)
def get_rate(frm, to): return convert(1, frm, to)

def db():
    c = sqlite3.connect(DB_PATH, timeout=30); c.row_factory = sqlite3.Row; return c
def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, paypal_email TEXT NOT NULL, pin_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deals (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, buyer_email TEXT NOT NULL, seller_email TEXT NOT NULL, seller_paypal_email TEXT NOT NULL, amount REAL NOT NULL, fee REAL NOT NULL, seller_gets REAL NOT NULL, orig_amount REAL, orig_currency TEXT DEFAULT 'USD', status TEXT DEFAULT 'WAITING_PAYMENT', is_delivered INTEGER DEFAULT 0, paypal_order_id TEXT, paypal_capture_id TEXT, payout_batch_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        """)
def hash_pin(p): return hashlib.sha256(str(p).encode()).hexdigest()
def current_user():
    if "email" not in session: return None
    with db() as con: return con.execute("SELECT * FROM users WHERE email=?", (session["email"],)).fetchone()
def paypal_token():
    auth = base64.b64encode(f"{PAYPAL_CLIENT_ID}:{PAYPAL_SECRET}".encode()).decode()
    r = requests.post(f"{PAYPAL_BASE}/v1/oauth2/token", data={"grant_type":"client_credentials"}, headers={"Authorization":f"Basic {auth}"}, timeout=20)
    r.raise_for_status(); return r.json()["access_token"]
def paypal_create_order(amount, deal_id):
    token = paypal_token()
    payload = {"intent":"CAPTURE","purchase_units":[{"amount":{"currency_code":"USD","value":f"{amount:.2f}"},"description":f"Tuma Escrow Deal #{deal_id}"}],"application_context":{"return_url":f"{os.environ.get('APP_URL','http://localhost:5000')}/paypal/success/{deal_id}","cancel_url":f"{os.environ.get('APP_URL','http://localhost:5000')}/paypal/cancel/{deal_id}"}}
    r = requests.post(f"{PAYPAL_BASE}/v2/checkout/orders", json=payload, headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}, timeout=20)
    data = r.json()
    if r.status_code not in [200,201]: return None, str(data)
    approve = [l["href"] for l in data.get("links",[]) if l["rel"]=="approve"]
    return {"order_id":data["id"],"approve_url":approve[0] if approve else ""}, "ok"
def paypal_capture(order_id):
    token = paypal_token()
    r = requests.post(f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture", headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}, timeout=20)
    return r.json(), r.status_code
def paypal_payout(to_email, amount, deal_id):
    token = paypal_token()
    batch_id = f"TUMA{deal_id}_{secrets.token_hex(4)}"
    payload = {"sender_batch_header":{"sender_batch_id":batch_id,"email_subject":"Tuma Escrow - Payment Released"},"items":[{"recipient_type":"EMAIL","amount":{"value":f"{amount:.2f}","currency":"USD"},"receiver":to_email,"note":f"Tuma Escrow Deal {deal_id}","sender_item_id":f"deal_{deal_id}"}]}
    r = requests.post(f"{PAYPAL_BASE}/v1/payments/payouts", json=payload, headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}, timeout=20)
    data = r.json()
    if r.status_code in [200,201]: return True, data.get("batch_header",{}).get("payout_batch_id","")
    return False, str(data)

# --- PWA ROUTES ---
@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")
@app.route("/sw.js")
def sw():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")
@app.route("/offline.html")
def offline():
    return "<h2 style='text-align:center;font-family:system-ui;padding:40px'>📴 You are offline<br><small>Check connection and try again. Your deals are safe.</small></h2>", 200

STYLE = """
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#003087">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="TUMA">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<link rel="icon" href="/static/icons/icon-192.png">
<style>
:root{font-family:system-ui,-apple-system,Arial;color:#18212f;background:#f4f7fb}*{box-sizing:border-box}body{margin:0;padding-bottom:80px}
header{background:#003087;color:white;padding:18px 14px 14px;text-align:center;position:sticky;top:0;z-index:10}
main{max-width:600px;margin:14px auto;padding:0 14px}
.panel{background:white;border:1px solid #dce4ee;border-radius:14px;padding:18px;margin:12px 0;box-shadow:0 2px 8px rgba(0,0,0,.04)}
button{background:#0070ba;color:white;border:0;border-radius:25px;padding:14px;font-size:1.05rem;cursor:pointer;width:100%;font-weight:700;min-height:48px}
.gold{background:#ffc439;color:#003087}.danger{background:#b42318}.btn2{background:#e7eef6;color:#173b5b}
input,select{width:100%;padding:13px;border:1px solid #bac8d8;border-radius:10px;margin:5px 0 10px;font-size:16px}
label{font-weight:600}.muted{color:#5b6878;font-size:.9rem}.success{background:#e4f7eb;padding:12px;border-radius:9px;color:#14532d}
.badge{padding:5px 10px;border-radius:20px;font-size:.85rem;font-weight:700;display:inline-block}
.WAITING_PAYMENT{background:#fff6d8}.HELD{background:#dbeafe}.RELEASED{background:#dcfce7}.DISPUTED{background:#fee2e2}.REFUNDED{background:#e5e7eb}
.deal-card{border:1px solid #dce4ee;border-radius:12px;padding:14px;margin:12px 0;background:#fff}
.balance{background:#003087;color:white;border-radius:14px;padding:18px;text-align:center}
.bottom-nav{position:fixed;bottom:0;left:0;right:0;background:white;border-top:1px solid #dce4ee;display:flex;justify-content:space-around;padding:10px 0 18px;z-index:20}
.bottom-nav a{text-decoration:none;color:#003087;font-weight:700;font-size:.85rem;text-align:center}
#installBtn{display:none;background:#ffc439;color:#003087;border:0;padding:10px 18px;border-radius:20px;font-weight:700;margin-top:10px}
</style>
<script>
let deferredPrompt;
window.addEventListener('beforeinstallprompt', (e)=>{e.preventDefault();deferredPrompt=e;document.getElementById('installBtn').style.display='inline-block';});
function installApp(){if(deferredPrompt){deferredPrompt.prompt();deferredPrompt.userChoice.then(()=>{deferredPrompt=null;document.getElementById('installBtn').style.display='none';});}else{alert('On iPhone: Tap Share ⬆️ then Add to Home Screen.\\nOn Android: Tap Menu ⋮ then Install App / Add to Home Screen.');}}
if('serviceWorker' in navigator){window.addEventListener('load',()=>{navigator.serviceWorker.register('/sw.js');});}
</script>
"""

AUTH_T = "<!doctype html><html><head><meta charset='utf-8'><title>TUMA PayPal</title>" + STYLE + "</head><body><header><h1 style='margin:0'>🛡️ TUMA</h1><div>PayPal Escrow | USD ⇄ KES ⇄ Any</div><button id='installBtn' onclick='installApp()'>📲 Install App</button></header><main>{% with m=get_flashed_messages() %}{% if m %}{% for x in m %}<div class='panel success'>{{x}}</div>{% endfor %}{% endif %}{% endwith %}<div class='panel'><h2>{{title}}</h2><form method='post'>{% if show %}<label>Your Name</label><input name='name' required placeholder='John Doe'><label>Your PayPal Email</label><input name='paypal_email' type='email' required placeholder='you@paypal.com'>{% endif %}<label>Email (login)</label><input name='email' type='email' required placeholder='you@gmail.com'><label>4-Digit PIN</label><input name='pin' type='password' inputmode='numeric' required placeholder='1234'><button>{{btn}}</button></form><p class='muted' style='text-align:center'>{{switch}}</p></div></main></body></html>"

DASH_T = "<!doctype html><html><head><meta charset='utf-8'><title>TUMA Dashboard</title>" + STYLE + "</head><body><header><h1 style='margin:0'>🛡️ TUMA</h1><div>Hi {{u.name}} | <a href='/logout' style='color:white'>Logout</a></div><button id='installBtn' onclick='installApp()'>📲 Install App</button></header><main>{% with m=get_flashed_messages() %}{% if m %}{% for x in m %}<div class='panel success'>{{x}}</div>{% endfor %}{% endif %}{% endwith %}<div style='text-align:center'><a href='/convert' style='background:#ffc439;color:#003087;padding:11px 22px;border-radius:20px;text-decoration:none;font-weight:700;display:inline-block'>💱 Convert - USD ⇄ KES</a></div><div class='panel' style='background:#eef6ff;text-align:center'><b>💱 Live:</b> 1 USD = {{kes_rate}} KES</div><div class='balance'><div>💰 Held For You</div><h1 style='font-size:2.2rem;margin:4px 0'>${{held}}</h1><div>≈ {{held_kes}} KES | Bought ${{bought}} | Sold ${{sold}}</div></div><div class='panel'><h2>Create Deal - Any Currency</h2><form method='post' action='/create'><label>What are you buying?</label><input name='title' required placeholder='MacBook Pro'><label>Seller Email</label><input name='seller_email' type='email' required placeholder='seller@gmail.com'><label>Seller PayPal Email</label><input name='seller_paypal' type='email' required placeholder='seller@paypal.com'><label>Amount</label><input name='amount' type='number' step='0.01' min='1' required placeholder='100'><label>Currency</label><select name='currency'><option value='USD'>USD - US Dollar</option><option value='KES'>KES - Kenya Shilling</option><option value='EUR'>EUR - Euro</option><option value='GBP'>GBP - British Pound</option><option value='NGN'>NGN - Naira</option><option value='UGX'>UGX - Uganda</option><option value='TZS'>TZS - Tanzania</option><option value='ZAR'>ZAR - Rand</option><option value='INR'>INR - Rupee</option><option value='CAD'>CAD - Dollar</option></select><p class='muted'>Fee 2% - Seller gets 98%. KES auto-converts to USD for PayPal.</p><button>Create Deal</button></form></div><div class='panel'><h2>My Deals</h2>{% if deals %}{% for d in deals %}<div class='deal-card'><b>#{{d.id}} {{d.title}}</b><br><span class='muted'>Buyer: {{d.buyer_email}}<br>Seller: {{d.seller_email}}<br>Original: <b>{{d.orig_amount}} {{d.orig_currency}}</b> = <b>${{d.amount}} USD</b><br>Seller gets ${{d.seller_gets}} USD (≈ {{d.seller_kes}} KES)</span><br><span class='badge {{d.status}}'>{{d.status}}</span><br>{% if d.is_delivered and d.status=='HELD' %}<span style='color:#0070ba;font-weight:700'>📦 Seller says DELIVERED</span><br>{% endif %}<br>{% if d.status=='WAITING_PAYMENT' and d.buyer_email==u.email %}<form method='post' action='/pay/{{d.id}}'><button class='gold'>Pay with PayPal - ${{d.amount}}</button></form>{% elif d.status=='HELD' and d.seller_email==u.email and not d.is_delivered %}<form method='post' action='/delivered/{{d.id}}'><button class='gold'>📦 I Have Delivered</button></form>{% elif d.status=='HELD' and d.buyer_email==u.email %}<form method='post' action='/release/{{d.id}}'><button>✅ I Received - Release</button></form><form method='post' action='/dispute/{{d.id}}' style='margin-top:8px'><button class='danger'>⚠️ Dispute</button></form>{% elif d.status=='DISPUTED' %}<form method='post' action='/admin-refund/{{d.id}}'><button class='btn2'>Admin: Refund Buyer</button></form><form method='post' action='/admin-release/{{d.id}}' style='margin-top:8px'><button class='btn2'>Admin: Pay Seller</button></form>{% endif %}</div>{% endfor %}{% else %}<p class='muted'>No deals yet.</p>{% endif %}</div></main><nav class='bottom-nav'><a href='/'>🏠<br>Home</a><a href='/convert'>💱<br>Convert</a><a href='/'>➕<br>New Deal</a><a href='/logout'>👤<br>Logout</a></nav></body></html>"

CONVERT_T = "<!doctype html><html><head><meta charset='utf-8'><title>Converter</title>" + STYLE + "</head><body><header><h1>💱 Converter</h1><div><a href='/' style='color:white'>Back</a></div></header><main><div class='panel'><h2>Convert Any Currency</h2><form method='post'><label>Amount</label><input name='amount' type='number' step='0.01' value='{{amt}}' required><label>From</label><select name='from_cur'>{% for c in currencies %}<option value='{{c}}' {% if fc==c %}selected{% endif %}>{{c}}</option>{% endfor %}</select><label>To</label><select name='to_cur'>{% for c in currencies %}<option value='{{c}}' {% if tc==c %}selected{% endif %}>{{c}}</option>{% endfor %}</select><button>Convert 🔄</button></form>{% if res %}<h2 style='background:#e4f7eb;padding:14px;border-radius:10px;text-align:center'>{{res}}</h2>{% endif %}</div><div class='panel'><h3>Quick</h3><p>1 USD = {{kes}} KES</p><p>100 USD = {{kes100}} KES</p><p>10,000 KES = {{kes_to_usd}} USD</p></div></main></body></html>"

init_db()
CURRENCIES = ["USD","KES","EUR","GBP","NGN","UGX","TZS","ZAR","INR","CAD","AUD","JPY","CHF","CNY","AED"]
@app.route("/")
def home():
    u = current_user()
    if not u: return redirect(url_for("login"))
    with db() as con:
        deals = con.execute("SELECT * FROM deals WHERE buyer_email=? OR seller_email=? ORDER BY id DESC", (u["email"], u["email"])).fetchall()
        held = con.execute("SELECT COALESCE(SUM(amount),0) FROM deals WHERE seller_email=? AND status='HELD'", (u["email"],)).fetchone()[0]
        bought = con.execute("SELECT COALESCE(SUM(amount),0) FROM deals WHERE buyer_email=? AND status IN ('HELD','RELEASED')", (u["email"],)).fetchone()[0]
        sold = con.execute("SELECT COALESCE(SUM(seller_gets),0) FROM deals WHERE seller_email=? AND status='RELEASED'", (u["email"],)).fetchone()[0]
    deals2 = []
    for d in deals:
        dd = dict(d); dd["seller_kes"] = convert(dd["seller_gets"], "USD", "KES"); deals2.append(dd)
    return render_template_string(DASH_T, u=u, deals=deals2, held=round(held,2), held_kes=convert(held,"USD","KES"), bought=round(bought,2), sold=round(sold,2), kes_rate=get_rate("USD","KES"))
@app.route("/convert", methods=["GET","POST"])
def convert_page():
    result=None; amount=100; frm="USD"; to="KES"; rate=0
    if request.method=="POST":
        amount=float(request.form.get("amount",100)); frm=request.form.get("from_cur","USD"); to=request.form.get("to_cur","KES")
        result=f"{amount} {frm} = {convert(amount,frm,to)} {to}"; rate=get_rate(frm,to)
    return render_template_string(CONVERT_T, res=result, amt=amount, fc=frm, tc=to, rate=rate, currencies=CURRENCIES, kes=get_rate("USD","KES"), kes100=convert(100,"USD","KES"), kes_to_usd=convert(10000,"KES","USD"))
@app.route("/api/convert")
def api_convert():
    try:
        amount=float(request.args.get("amount",1)); frm=request.args.get("from","USD"); to=request.args.get("to","KES")
        return jsonify({"amount":amount,"from":frm,"to":to,"converted":convert(amount,frm,to),"rate":get_rate(frm,to)})
    except Exception as e: return jsonify({"error":str(e)}),400
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method=="POST":
        try:
            name=request.form.get("name","").strip(); email=request.form.get("email","").strip().lower(); paypal_email=request.form.get("paypal_email","").strip().lower(); pin=request.form.get("pin","").strip()
            now=datetime.utcnow().isoformat()
            with db() as con: con.execute("INSERT INTO users (name,email,paypal_email,pin_hash,created_at) VALUES (?,?,?,?,?)", (name,email,paypal_email,hash_pin(pin),now))
            session["email"]=email; flash("Account created! Welcome to TUMA."); return redirect(url_for("home"))
        except Exception as e: flash("Email already registered. Login." if "UNIQUE" in str(e) else str(e))
    return render_template_string(AUTH_T, title="Create Account", btn="Create Account", show=True, switch="<a href='/login'>Already have account? Login</a>")
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        email=request.form.get("email","").strip().lower(); pin=request.form.get("pin","")
        with db() as con: u=con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u or u["pin_hash"]!=hash_pin(pin): flash("Wrong email or PIN")
        else: session["email"]=email; return redirect(url_for("home"))
    return render_template_string(AUTH_T, title="Login", btn="Login", show=False, switch="<a href='/register'>No account? Create one</a>")
@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("login"))
@app.route("/create", methods=["POST"])
def create():
    u=current_user()
    if not u: return redirect(url_for("login"))
    try:
        title=request.form.get("title","").strip()[:100]; seller_email=request.form.get("seller_email","").strip().lower(); seller_paypal=request.form.get("seller_paypal","").strip().lower(); orig_amount=float(request.form.get("amount",0)); currency=request.form.get("currency","USD").upper()
        usd_amount=convert(orig_amount,currency,"USD"); fee=round(usd_amount*0.02,2); seller_gets=round(usd_amount-fee,2); now=datetime.utcnow().isoformat()
        with db() as con: con.execute("INSERT INTO deals (title,buyer_email,seller_email,seller_paypal_email,amount,fee,seller_gets,orig_amount,orig_currency,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?, 'WAITING_PAYMENT',?,?)", (title,u["email"],seller_email,seller_paypal,usd_amount,fee,seller_gets,orig_amount,currency,now,now))
        flash(f"Deal created! {orig_amount} {currency} = ${usd_amount} USD. Tap Pay with PayPal.")
    except Exception as e: flash(str(e))
    return redirect(url_for("home"))
@app.route("/pay/<int:deal_id>", methods=["POST"])
def pay(deal_id):
    with db() as con: deal=con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not deal or deal["status"]!="WAITING_PAYMENT": flash("Not ready"); return redirect(url_for("home"))
    if not PAYPAL_CLIENT_ID: flash("Add PAYPAL_CLIENT_ID in Render first"); return redirect(url_for("home"))
    res,msg=paypal_create_order(deal["amount"],deal_id)
    if not res: flash(f"PayPal error: {msg}"); return redirect(url_for("home"))
    with db() as con: con.execute("UPDATE deals SET paypal_order_id=?, updated_at=? WHERE id=?", (res["order_id"], datetime.utcnow().isoformat(), deal_id))
    return redirect(res["approve_url"])
@app.route("/paypal/success/<int:deal_id>")
def paypal_success(deal_id):
    order_id=request.args.get("token"); data,code=paypal_capture(order_id)
    if code not in [200,201]: flash("PayPal capture failed"); return redirect(url_for("home"))
    cap=""
    try: cap=data["purchase_units"][0]["payments"]["captures"][0]["id"]
    except: pass
    with db() as con: con.execute("UPDATE deals SET status='HELD', paypal_capture_id=?, updated_at=? WHERE id=?", (cap, datetime.utcnow().isoformat(), deal_id))
    flash("Payment HELD! Seller should deliver now."); return redirect(url_for("home"))
@app.route("/paypal/cancel/<int:deal_id>")
def paypal_cancel(deal_id): flash("PayPal payment cancelled."); return redirect(url_for("home"))
@app.route("/delivered/<int:deal_id>", methods=["POST"])
def delivered(deal_id):
    with db() as con: con.execute("UPDATE deals SET is_delivered=1, updated_at=? WHERE id=?", (datetime.utcnow().isoformat(), deal_id))
    flash("Buyer notified - you delivered!"); return redirect(url_for("home"))
@app.route("/release/<int:deal_id>", methods=["POST"])
def release(deal_id):
    with db() as con: deal=con.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not deal or deal["status"]!="HELD": flash("Not HELD"); return redirect(url_for("home"))
    ok,info=paypal_payout(deal["seller_paypal_email"], deal["seller_gets"], deal_id); now=datetime.utcnow().isoformat()
    with db() as con:
        if ok: con.execute("UPDATE deals SET status='RELEASED', payout_batch_id=?, updated_at=? WHERE id=?", (info, now, deal_id)); flash(f"Released! ${deal['seller_gets']} sent to seller.")
        else: con.execute("UPDATE deals SET status='RELEASED', payout_batch_id='MANUAL', updated_at=? WHERE id=?", (now, deal_id)); flash(f"Released! Send ${deal['seller_gets']} manually to {deal['seller_paypal_email']}.")
    return redirect(url_for("home"))
@app.route("/dispute/<int:deal_id>", methods=["POST"])
def dispute(deal_id):
    with db() as con: con.execute("UPDATE deals SET status='DISPUTED', updated_at=? WHERE id=?", (datetime.utcnow().isoformat(), deal_id))
    flash("Disputed. Money stays HELD."); return redirect(url_for("home"))
@app.route("/admin-refund/<int:deal_id>", methods=["POST"])
def admin_refund(deal_id):
    with db() as con: con.execute("UPDATE deals SET status='REFUNDED', updated_at=? WHERE id=?", (datetime.utcnow().isoformat(), deal_id))
    flash("Admin: Marked REFUNDED. Refund in PayPal > Activity > Refund."); return redirect(url_for("home"))
@app.route("/admin-release/<int:deal_id>", methods=["POST"])
def admin_release(deal_id): return release(deal_id)
@app.route("/health")
def health(): return jsonify({"status":"ok","app":"Tuma PayPal PWA","mode":PAYPAL_MODE,"usd_to_kes":get_rate("USD","KES")})
if __name__=="__main__":
    init_db(); app.run(host="0.0.0.0", port=int(os.environ.get("PORT","5000")))
