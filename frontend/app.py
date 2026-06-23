import streamlit as st
import json
import os
import time
import requests

st.set_page_config(page_title="AI OS", layout="centered", initial_sidebar_state="collapsed")

st.markdown("<style>#MainMenu {visibility: hidden;} footer {visibility: hidden;} header {visibility: hidden;}</style>", unsafe_allow_html=True)
st.title("✨ Workspace Assistant")
st.caption("Secure Local Environment | Employee ID: 001")

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "session_id" not in st.session_state:
    import uuid
    st.session_state.session_id = str(uuid.uuid4())


DRAFTS_FILE = "/home/my_vm_google/projects/frontend/email_drafts.json"

def remove_draft(index_to_remove):
    if os.path.exists(DRAFTS_FILE):
        with open(DRAFTS_FILE, "r") as f:
            drafts = f.readlines()
        if index_to_remove < len(drafts):
            drafts.pop(index_to_remove)
            with open(DRAFTS_FILE, "w") as f:
                f.writelines(drafts)

with st.chat_message("assistant", avatar="✨"):
    st.write("Good afternoon. I've monitored your inbox while you were away.")
    
    if os.path.exists(DRAFTS_FILE):
        with open(DRAFTS_FILE, "r") as f:
            drafts = f.readlines()
            
        if drafts:
            st.info(f"📥 You have **{len(drafts)}** pending email draft(s) awaiting your approval.")
            for i, draft_line in enumerate(reversed(drafts)):
                real_idx = len(drafts) - 1 - i 
                draft = json.loads(draft_line)
                
                with st.expander(f"📧 Review Reply to: {draft.get('sender', 'Unknown')}", expanded=(i==0)):
                    st.caption(f"**Subject:** {draft.get('subject', 'No Subject')}")
                    st.markdown(f"**Triage Summary:** _{draft.get('triage_notes', '')}_")
                    
                    final_email = st.text_area("Edit Response Draft:", value=draft.get("draft_reply", ""), height=200, key=f"text_{real_idx}")
                    
                    if st.button("✅ Send Real Email", type="primary", key=f"send_{real_idx}"):
                        with st.spinner("Transmitting outbound mail via network..."):
                            payload = {
                                "to_email": draft.get("sender", ""),
                                "subject": draft.get("subject", ""),
                                "body": final_email
                            }
                            send_response = requests.post("http://localhost:8000/send_email", json=payload).json()
                            
                            if send_response.get("status") == "success":
                                remove_draft(real_idx)
                                st.success("Email successfully sent and delivered!")
                                time.sleep(1)
                                st.rerun()
                            else:
                                st.error(f"Network Outbox Error: {send_response.get('message')}")
        else:
            st.success("Inbox is at zero. No pending actions.")
    else:
        st.success("Inbox is at zero. No pending actions.")

# Render existing chat history
for msg in st.session_state.chat_history:
    avatar = "👤" if msg["role"] == "user" else "✨"
    with st.chat_message(msg["role"], avatar=avatar):
        st.write(msg["content"])

if user_input := st.chat_input("Ask your assistant..."):
    # Append user message to state
    st.session_state.chat_history.append({"role": "user", "content": user_input})
    
    # Display user message immediately
    with st.chat_message("user", avatar="👤"):
        st.write(user_input)
        
    # Query backend /chat endpoint
    with st.chat_message("assistant", avatar="✨"):
        with st.spinner("Thinking..."):
            try:
                payload = {
                    "message": user_input,
                    "session_id": st.session_state.session_id
                }
                response = requests.post("http://localhost:8000/chat", json=payload)
                if response.status_code == 200:
                    reply = response.json().get("reply", "No reply received.")
                else:
                    reply = f"Error: Backend returned status status code {response.status_code}."
            except Exception as e:
                reply = f"Connection error: {e}"
            
            st.write(reply)
            st.session_state.chat_history.append({"role": "assistant", "content": reply})
    st.rerun()