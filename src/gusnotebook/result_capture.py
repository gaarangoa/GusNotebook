"""Small, dependency-free capture helper sent to an arbitrary Python kernel."""

BOOTSTRAP = '''
def _gusnotebook_capture_result(name):
    import math
    from IPython.display import JSON
    remaining = [4096]
    characters = [65536]
    def clean(value, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 12:
            raise ValueError("Published result exceeds 4096 items or 12 nested levels")
        if type(value).__module__.startswith("numpy") and hasattr(value, "item") and getattr(value, "ndim", 1) == 0:
            value = value.item()
        if value is None or type(value) in (bool, int):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if type(value) is str and len(value) <= 16384:
            characters[0] -= len(value)
            if characters[0] < 0:
                raise ValueError("Published result exceeds 65536 characters")
            return value
        if type(value) in (list, tuple):
            return [clean(item, depth + 1) for item in value]
        if type(value) is dict and all(type(key) is str and len(key) <= 1024 for key in value):
            characters[0] -= sum(map(len, value))
            if characters[0] < 0:
                raise ValueError("Published result exceeds 65536 characters")
            return {key: clean(item, depth + 1) for key, item in value.items()}
        raise ValueError("Publish finite JSON scalars, lists or records; use display outputs for tables and figures")
    return JSON({"value": clean(get_ipython().user_ns[name])})
'''


def collect(reply, names):
    content = (reply or {}).get("content", {})
    values, errors = {}, {}
    for name in names:
        result = content.get("user_expressions", {}).get(name, {})
        data = result.get("data", {}).get("application/json", {})
        if result.get("status") == "ok" and isinstance(data, dict) and "value" in data:
            values[name] = data["value"]
        else:
            errors[name] = result.get("evalue", "Result was not captured")
    return {"status": content.get("status", "unknown"), "values": values, "errors": errors}
