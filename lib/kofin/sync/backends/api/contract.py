"""Pure public-interface floor; behavioral qualification is separate."""


def version(value):
    return tuple(int(part) for part in value.split("."))


def check(capture, contract, allow_dirty=False):
    capture = capture.get("result", capture)
    errors = []
    application = capture.get("application", {}).get("version", {})
    if application.get("major", 0) < contract["minimum_kodi_major"]:
        errors.append("Kodi 22 Piers or later is required")
    for addon, minimum in contract["system_addons"].items():
        try:
            adequate = version(
                capture.get("system_addons", {}).get(addon, "0")
            ) >= version(minimum)
        except ValueError:
            adequate = False
        if not adequate:
            errors.append(addon + " >= " + minimum + " is required")
    dirty = "dirty" in application.get("revision", "").lower()
    if dirty and not allow_dirty:
        errors.append(
            "dirty build requires explicit phase-0 exception; not stock qualification"
        )
    for method, parameters in contract["methods"].items():
        definition = capture.get("methods", {}).get(method)
        if definition is None:
            errors.append("missing method " + method)
            continue
        actual = {param["name"]: param for param in definition.get("params", [])}
        for name, required in parameters.items():
            if name not in actual:
                errors.append("missing parameter " + method + "." + name)
            elif required and not set(required).issubset(actual[name].get("enums", [])):
                errors.append("missing enum values " + method + "." + name)
    for field, methods in contract["python"].items():
        for method in methods:
            if capture.get(field, {}).get(method) is not True:
                errors.append("missing Python capability " + field + "." + method)
    return {
        "interfaces_passed": not errors,
        "errors": errors,
        "dirty_build_exception_used": dirty and allow_dirty,
        "stock_release_qualified": False,
        "behavioral_gates": contract["behavioral_gates"],
    }
