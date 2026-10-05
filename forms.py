"""Interactive forms that can be added to an envelope next to PDFs.

A form has no pages of its own: the assigned signer fills it in on the signing
page, and when the envelope completes it is rendered as PDF pages (with that
signer's signature) inside the completed document.
"""

ROOM_SUGGESTIONS = [
    "Kitchen", "Primary bathroom", "Main bathroom", "Ensuite", "Powder room", "Laundry room",
    "Mudroom", "Basement", "Basement bathroom", "Wet bar", "Butler's pantry", "Garage", "Outdoor kitchen",
    "Guest bathroom", "Kids' bathroom",
]

CATEGORIES = {
    "Plumbing": [
        "Kitchen sink", "Kitchen faucet", "Bar / prep sink", "Bar / prep faucet", "Pot filler",
        "Bathroom sink", "Bathroom faucet", "Toilet", "Bidet / bidet seat", "Bathtub", "Tub filler",
        "Shower valve / trim", "Shower head", "Hand shower", "Body sprays", "Shower drain",
        "Laundry / utility sink", "Laundry faucet", "Garbage disposal", "Instant hot water dispenser",
        "Water filtration", "Water heater", "Water softener", "Hose bib", "Other plumbing",
    ],
    "Appliance": [
        "Range / stove", "Cooktop", "Wall oven", "Speed / steam oven", "Microwave", "Range hood",
        "Refrigerator", "Freezer", "Beverage / wine fridge", "Ice maker", "Dishwasher",
        "Warming drawer", "Coffee system", "Trash compactor", "Washer", "Dryer", "Other appliance",
    ],
}

SPEC_FORM = {
    "key": "room_specs",
    "name": "Plumbing & appliance specifications",
    "description": "Add each room, then the plumbing fixtures and appliances going into it, with make and model.",
    "max_rooms": 20,
    "max_items": 40,
    "categories": CATEGORIES,
    "room_suggestions": ROOM_SUGGESTIONS,
}

FORMS = {SPEC_FORM["key"]: SPEC_FORM}


def public_def(key):
    return FORMS.get(key)


def _s(v, n):
    return str(v or "").strip()[:n]


def validate(key, data):
    """Return (clean_answers, error_message_or_None)."""
    if key != "room_specs":
        return None, "Unknown form."
    d = FORMS[key]
    if not isinstance(data, dict):
        return None, "Fill in the specification form."
    rooms_in = data.get("rooms") if isinstance(data.get("rooms"), list) else []
    if not rooms_in:
        return None, "Add at least one room to the specification form."
    if len(rooms_in) > d["max_rooms"]:
        return None, f"The specification form allows up to {d['max_rooms']} rooms."
    valid_types = {t: c for c, ts in d["categories"].items() for t in ts}
    rooms = []
    for i, r in enumerate(rooms_in, 1):
        r = r if isinstance(r, dict) else {}
        name = _s(r.get("name"), 80)
        if not name:
            return None, f"Give room {i} a name."
        items_in = r.get("items") if isinstance(r.get("items"), list) else []
        if not items_in:
            return None, f"Add at least one item to “{name}”."
        if len(items_in) > d["max_items"]:
            return None, f"“{name}” has too many items (max {d['max_items']})."
        items = []
        for j, it in enumerate(items_in, 1):
            it = it if isinstance(it, dict) else {}
            t = _s(it.get("type"), 60)
            if t not in valid_types:
                return None, f"Choose what item {j} in “{name}” is (for example Sink or Stove)."
            make, model = _s(it.get("make"), 80), _s(it.get("model"), 80)
            if not make or not model:
                return None, f"Add the make and model for the {t.lower()} in “{name}”."
            items.append({"category": valid_types[t], "type": t, "make": make, "model": model,
                          "finish": _s(it.get("finish"), 60), "qty": max(1, min(int(it.get("qty") or 1), 99))
                          if str(it.get("qty") or "1").isdigit() else 1,
                          "notes": _s(it.get("notes"), 300)})
        rooms.append({"name": name, "items": items})
    return {"rooms": rooms, "notes": _s(data.get("notes"), 1000)}, None
