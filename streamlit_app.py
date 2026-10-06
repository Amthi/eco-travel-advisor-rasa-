"""
Optional Streamlit prototype frontend (use frontend/index.html as the main UI).

Run in its OWN virtual env (see requirements-frontend.txt):
    streamlit run streamlit_app.py
Rasa must be running:  rasa run --cors "*" -p 5005
"""
import uuid

import requests
import streamlit as st

RASA_REST_URL = "http://localhost:5005/webhooks/rest/webhook"
STYLE = {  # colour, background, text label (colour is never the only cue)
    "low": ("#1b7f3b", "#e6f4ea", "LOW IMPACT"),
    "medium": ("#b45309", "#fff4e0", "MODERATE IMPACT"),
    "high": ("#b91c1c", "#fdeaea", "HIGH IMPACT"),
    "unknown": ("#555555", "#eeeeee", "UNKNOWN"),
}


def badge(category):
    colour, _, label = STYLE.get(category, STYLE["unknown"])
    return (f'<span style="background:{colour};color:#fff;padding:2px 10px;border-radius:12px;'
            f'font-size:.75rem;font-weight:700;white-space:nowrap">{label}</span>')

st.set_page_config(page_title="Eco-Travel Advisor", page_icon="🌍")
st.title("🌍 Eco-Travel Advisor")

ss = st.session_state
ss.setdefault("sender", f"st-{uuid.uuid4().hex[:8]}")
ss.setdefault("history", [])      # list of (kind, content)
ss.setdefault("handover", False)
ss.setdefault("started", False)


def send(message: str):
    try:
        r = requests.post(RASA_REST_URL, json={"sender": ss.sender, "message": message}, timeout=10)
        r.raise_for_status()
        replies = r.json()
    except (requests.RequestException, ValueError):
        ss.history.append(("bot", "⚠️ I couldn't reach the assistant service."))
        return
    for m in replies:
        if m.get("text"):
            ss.history.append(("bot", m["text"]))
        c = m.get("custom")
        if c:
            if c.get("type") == "handover":
                ss.handover = True
            ss.history.append(("custom", c))
        if m.get("buttons"):
            ss.history.append(("buttons", m["buttons"]))


if not ss.started:
    ss.started = True
    send("/greet")

if ss.handover:
    st.info("✅ Connected to a human travel specialist, who has the full context.")

last_buttons_index = max((i for i, (k, _) in enumerate(ss.history) if k == "buttons"), default=-1)
for i, (kind, content) in enumerate(ss.history):
    if kind == "user":
        st.chat_message("user").write(content)
    elif kind == "bot":
        st.chat_message("assistant").write(content)
    elif kind == "custom":
        with st.chat_message("assistant"):
            if content.get("type") == "carbon_card":
                if content.get("high_emission_alert"):
                    st.warning("⚠️ " + (content.get("alert_text") or "High-emission option(s) on this route."))
                for o in content["options"]:
                    colour, background, _ = STYLE.get(o["category"], STYLE["unknown"])
                    st.markdown(
                        f'<div role="group" aria-label="{o["label"]}: {o["co2e_kg"]} kg CO2e" '
                        f'style="border-left:8px solid {colour};background:{background};padding:8px 12px;margin:6px 0;'
                        f'border-radius:6px;display:flex;justify-content:space-between;align-items:center;color:#111">'
                        f'<b>{o["label"]}</b><span>{o["co2e_kg"]} kg CO₂e ({o["distance_km"]} km) &nbsp;{badge(o["category"])}</span></div>',
                        unsafe_allow_html=True,
                    )
            elif content.get("type") == "hotel_carousel":
                cols = st.columns(len(content["hotels"]) or 1)
                for col, h in zip(cols, content["hotels"]):
                    with col.container(border=True):
                        st.markdown(f"**{h['name']}**")
                        st.write(f"€{h.get('price')} / night")
                        cat = h.get("category", "unknown")
                        st.markdown("Carbon: " + badge(cat), unsafe_allow_html=True)  # separate from verification
                        if h.get("carbon_kg_per_night") is not None:
                            st.caption(f"≈ {h['carbon_kg_per_night']} kg CO₂e per night")
                        v = h.get("verification")
                        st.caption("🧪 demo data, certification NOT verified" if v == "demo"
                                   else "✅ verified" if v == "verified" else "⚠️ certification unverified")
                        if h.get("trip_total_kg_co2e") is not None:
                            st.caption(f"Trip total ≈ {h['trip_total_kg_co2e']} kg CO₂e"
                                       + (f" ({content['nights']} nights)" if content.get("nights") else ""))
    elif kind == "buttons" and i == last_buttons_index:
        cols = st.columns(len(content))
        for n, (col, b) in enumerate(zip(cols, content)):
            if col.button(b["title"], key=f"b{i}-{n}"):
                ss.history.append(("user", b["title"]))
                send(b["payload"])
                st.rerun()

text = st.chat_input("Type a message...")
if text:
    ss.history.append(("user", text))
    send(text)
    st.rerun()
