Smart Commissioning App - Windows Portable Tester Build

Purpose
This package lets engineers dry-run the Smart Commissioning App on a Windows PC without installing Python, Node.js, npm, Docker, Redis, or PostgreSQL.

What is included
- SmartCommissioningApp.exe: local launcher for the app.
- backend/: FastAPI backend source used by the launcher.
- frontend/dist/: built React app.
- runtime/: created automatically for local runs, saved run records, and local secret placeholders.

How to use
1. Extract the zip to a normal folder, for example Desktop\Smart_Commissioning_App_Windows_Portable.
2. Open the extracted folder.
3. Double-click SmartCommissioningApp.exe.
4. Keep the black console window open while testing.
5. The app should open automatically in your browser. If it does not, copy the URL printed in the console, usually http://127.0.0.1:8000/.
6. Use the Review Comments button in the bottom-right corner of the app to leave testing feedback.
7. Export comments as JSON or CSV from that same feedback panel and send the exported file back.

Important notes
- Do not open only http://127.0.0.1:8000 from an old backend process. Use the URL printed by SmartCommissioningApp.exe.
- If port 8000 is already busy, the launcher automatically uses the next available local port and prints the correct URL.
- Windows SmartScreen may warn because this is an internal unsigned tester build. Choose More info, then Run anyway only if you received the zip from the project owner.
- Corporate antivirus may scan the folder during first launch; first startup can take longer.
- Stop the app by pressing Ctrl+C in the console window or closing the console.

Feedback requested
- Confirm the homepage explains the app clearly.
- Walk through Configuration, MQTT Settings/Discovery, UDMI Validation, Reports, and the review comments workflow.
- Try the default config publish and validation examples.
- Record any UI/UX issue, unclear wording, missing workflow, failed action, or expected commissioning behavior that is not represented.
