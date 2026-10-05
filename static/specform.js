// Room-by-room plumbing & appliance spec form. Used on the signing page (interactive)
// and in the editor (read-only preview).
window.SpecForm = (function () {
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  let uidN = 0; const uid = () => 'sf' + (++uidN);

  function mount(root, def, opts = {}) {
    const preview = !!opts.preview;
    const state = { rooms: [], notes: '' };
    const listId = uid() + '-rooms';
    const typeOptions = Object.entries(def.categories).map(([cat, types]) =>
      `<optgroup label="${esc(cat)}">${types.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('')}</optgroup>`).join('');

    root.classList.add('spec');
    root.innerHTML = `
      <div class="spec-head">
        <div><span class="eyebrow">Online form</span><h2>${esc(def.name)}</h2>
          <p class="muted small">${esc(def.description)}</p></div>
        <span class="spec-count" aria-live="polite"></span>
      </div>
      <datalist id="${listId}">${def.room_suggestions.map(r => `<option value="${esc(r)}">`).join('')}</datalist>
      <div class="spec-rooms"></div>
      <div class="spec-add-room-wrap">
        <button type="button" class="btn btn-primary spec-add-room">+ Add room</button>
        <span class="muted small spec-limit"></span>
      </div>
      <label class="spec-notes">General notes <span class="hint">Optional: anything else the team should know</span>
        <textarea rows="3" maxlength="1000"></textarea></label>
      <p class="spec-error field-error" role="alert"></p>`;
    const roomsEl = root.querySelector('.spec-rooms');
    const addRoomBtn = root.querySelector('.spec-add-room');
    const notesEl = root.querySelector('.spec-notes textarea');
    if (preview) root.querySelectorAll('input,select,textarea,button').forEach(el => el.disabled = true);

    function changed() {
      // clear old validation messages as soon as the client edits anything
      root.querySelector('.spec-error').textContent = '';
      root.querySelectorAll('.invalid').forEach(x => x.classList.remove('invalid'));
      paintCount(); if (opts.onChange) opts.onChange();
    }
    function paintCount() {
      const n = state.rooms.length;
      root.querySelector('.spec-count').textContent = `${n} of ${def.max_rooms} rooms`;
      addRoomBtn.disabled = preview || n >= def.max_rooms;
      root.querySelector('.spec-limit').textContent = n >= def.max_rooms ? `Maximum of ${def.max_rooms} rooms reached.` : '';
      roomsEl.querySelectorAll('.spec-room').forEach((el, i) => { el.querySelector('.spec-room-num').textContent = `Room ${i + 1}`; });
    }

    function addRoom(data) {
      if (state.rooms.length >= def.max_rooms) return null;
      const room = { name: (data && data.name) || '', items: [] };
      state.rooms.push(room);
      const el = document.createElement('section');
      el.className = 'spec-room';
      el.innerHTML = `
        <div class="spec-room-head">
          <span class="spec-room-num"></span>
          <input type="text" class="spec-room-name" list="${listId}" maxlength="80" placeholder="Room name, e.g. Kitchen" aria-label="Room name">
          <button type="button" class="btn btn-quiet small spec-toggle" aria-expanded="true">Collapse</button>
          <button type="button" class="btn btn-quiet small btn-danger spec-del-room">Remove room</button>
        </div>
        <div class="spec-room-body">
          <div class="spec-items"></div>
          <button type="button" class="btn spec-add-item">+ Add plumbing or appliance</button>
        </div>`;
      const nameIn = el.querySelector('.spec-room-name');
      nameIn.value = room.name;
      nameIn.addEventListener('input', () => { room.name = nameIn.value; changed(); });
      el.querySelector('.spec-del-room').addEventListener('click', () => {
        if ((room.items.some(i => i.make || i.model) || room.name) && !confirm(`Remove ${room.name || 'this room'} and its items?`)) return;
        state.rooms.splice(state.rooms.indexOf(room), 1); el.remove(); changed();
      });
      const toggle = el.querySelector('.spec-toggle');
      toggle.addEventListener('click', () => {
        const open = toggle.getAttribute('aria-expanded') === 'true';
        toggle.setAttribute('aria-expanded', !open); toggle.textContent = open ? 'Expand' : 'Collapse';
        el.querySelector('.spec-room-body').hidden = open;
        el.classList.toggle('collapsed', open);
      });
      const itemsEl = el.querySelector('.spec-items');
      el.querySelector('.spec-add-item').addEventListener('click', () => {
        if (room.items.length >= def.max_items) return;
        const row = addItem(room, itemsEl); changed(); row.querySelector('select').focus();
      });
      ((data && data.items) || [{}]).forEach(it => addItem(room, itemsEl, it));
      roomsEl.appendChild(el);
      if (preview) el.querySelectorAll('input,select,textarea,button').forEach(x => x.disabled = true);
      return el;
    }

    function addItem(room, itemsEl, data) {
      const item = Object.assign({ type: '', make: '', model: '', finish: '', qty: 1, notes: '' }, data || {});
      room.items.push(item);
      const row = document.createElement('div');
      row.className = 'spec-item';
      const id = uid();
      row.innerHTML = `
        <label class="si-type" for="${id}t"><span>Item</span><select id="${id}t"><option value="">Choose…</option>${typeOptions}</select></label>
        <label class="si-make" for="${id}m"><span>Make</span><input id="${id}m" type="text" maxlength="80" placeholder="e.g. Kohler"></label>
        <label class="si-model" for="${id}o"><span>Model</span><input id="${id}o" type="text" maxlength="80" placeholder="Model number"></label>
        <label class="si-finish" for="${id}f"><span>Finish / colour <em>optional</em></span><input id="${id}f" type="text" maxlength="60" placeholder="e.g. Matte black"></label>
        <label class="si-qty" for="${id}q"><span>Qty</span><input id="${id}q" type="number" min="1" max="99" inputmode="numeric"></label>
        <label class="si-notes" for="${id}n"><span>Notes <em>optional</em></span><input id="${id}n" type="text" maxlength="300" placeholder="Size, side, special install…"></label>
        <button type="button" class="btn btn-quiet small btn-danger si-del" aria-label="Remove item">✕</button>`;
      const q = s => row.querySelector(s);
      q('select').value = item.type; q('.si-make input').value = item.make; q('.si-model input').value = item.model;
      q('.si-finish input').value = item.finish; q('.si-qty input').value = item.qty; q('.si-notes input').value = item.notes;
      const bind = (sel, key, num) => q(sel).addEventListener('input', e => { item[key] = num ? parseInt(e.target.value || '1', 10) : e.target.value; row.classList.remove('invalid'); changed(); });
      bind('select', 'type'); bind('.si-make input', 'make'); bind('.si-model input', 'model');
      bind('.si-finish input', 'finish'); bind('.si-qty input', 'qty', true); bind('.si-notes input', 'notes');
      q('select').addEventListener('change', e => { item.type = e.target.value; row.dataset.cat = e.target.selectedOptions[0]?.parentElement?.label || ''; changed(); });
      row.dataset.cat = q('select').selectedOptions[0]?.parentElement?.label || '';
      q('.si-del').addEventListener('click', () => {
        if (room.items.length === 1) { setError('Each room needs at least one item. Remove the room instead.'); return; }
        room.items.splice(room.items.indexOf(item), 1); row.remove(); changed();
      });
      itemsEl.appendChild(row);
      return row;
    }

    function setError(msg, focusEl) {
      root.querySelector('.spec-error').textContent = msg || '';
      if (focusEl) { focusEl.scrollIntoView({ block: 'center', behavior: 'smooth' }); focusEl.focus({ preventScroll: true }); }
    }

    function validate() {
      root.querySelectorAll('.invalid').forEach(x => x.classList.remove('invalid'));
      if (!state.rooms.length) return fail('Add at least one room.', addRoomBtn);
      const roomEls = [...roomsEl.querySelectorAll('.spec-room')];
      for (let i = 0; i < state.rooms.length; i++) {
        const r = state.rooms[i], rel = roomEls[i];
        if (!r.name.trim()) { rel.classList.add('invalid'); return fail(`Give room ${i + 1} a name.`, rel.querySelector('.spec-room-name')); }
        const rows = [...rel.querySelectorAll('.spec-item')];
        for (let j = 0; j < r.items.length; j++) {
          const it = r.items[j], row = rows[j];
          const open = () => { rel.querySelector('.spec-room-body').hidden = false; };
          if (!it.type) { open(); row.classList.add('invalid'); return fail(`Choose what item ${j + 1} in ${r.name} is.`, row.querySelector('select')); }
          if (!it.make.trim()) { open(); row.classList.add('invalid'); return fail(`Add the make of the ${it.type.toLowerCase()} in ${r.name}.`, row.querySelector('.si-make input')); }
          if (!it.model.trim()) { open(); row.classList.add('invalid'); return fail(`Add the model of the ${it.type.toLowerCase()} in ${r.name}.`, row.querySelector('.si-model input')); }
        }
      }
      setError('');
      return { ok: true };
    }
    function fail(message, el) { return { ok: false, message, el }; }
    function isComplete() {
      return state.rooms.length > 0 && state.rooms.every(r => r.name.trim() && r.items.length && r.items.every(i => i.type && i.make.trim() && i.model.trim()));
    }

    notesEl.addEventListener('input', () => { state.notes = notesEl.value; });
    addRoomBtn.addEventListener('click', () => { const el = addRoom(); changed(); if (el) { el.scrollIntoView({ block: 'center', behavior: 'smooth' }); el.querySelector('.spec-room-name').focus({ preventScroll: true }); } });

    (opts.initial && opts.initial.rooms && opts.initial.rooms.length ? opts.initial.rooms : [{}]).forEach(r => addRoom(r));
    paintCount();
    return {
      value: () => ({ rooms: state.rooms.map(r => ({ name: r.name.trim(), items: r.items.map(i => ({ ...i })) })), notes: state.notes }),
      validate, isComplete, showError: setError,
    };
  }
  return { mount };
})();
