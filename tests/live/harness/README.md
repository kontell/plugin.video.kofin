# In-Kodi harnesses

Scripts run *inside* Kodi with the `RunScript(...)` builtin (EventServer: `kodi-builtin 'RunScript(/abs/path/file.py,arg,...)'`; for the flatpak Piers export `KODI_ESPORT=9778` and copy the file somewhere its sandbox can see — `~/.var/app/tv.kodi.Kodi/data/harness/`). They import the installed add-on's own modules, so a guarded IPC message is sent through `kofin.core.ipc.notify` (the add-on reads its nonce itself) and a sign-in goes through `kofin.core.auth` exactly as `plugin/account.py` does. Nothing secret is printed: passwords are read from `~/.config/kodi-drive/targets.env` inside the Kodi process.

- `kofin_ipc.py` — `RunScript(...,RepairLibrary,<id>,<id>)`, `RunScript(...,UpdateLibrary,Id=<id>)`, any registered method.
- `kofin_login.py` — `RunScript(...,login,http://127.0.0.1:8098,kofin-test,JF12_TEST_PW)`, `RunScript(...,whitelist,Movies,Shows,Music)`, `RunScript(...,status)`.

Kodi logs `CPythonInvoker: Script invoked without an addon` for these — harmless. Results land in `kodi.log` under the `kofin-harness:` prefix.
