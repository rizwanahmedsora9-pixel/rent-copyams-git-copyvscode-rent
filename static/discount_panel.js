/* =========================================================================
   Per-item discount panel.

   The bill keeps ONE discount, but the profit report needs a discount per
   line.  Instead of the report dividing that amount later (which is what
   produced values like 4,756.0975609756 for a 5,000 rent line), the user
   chooses the items here and the exact amounts are sent with the form:

       item_discount_name[]   the item's name
       item_discount_value[]  the amount typed against it

   The server (app.services.pricing) converts everything to whole paisa with
   largest-remainder rounding and stores it on the document line, so the parts
   always add back to the bill discount exactly.

   Usage: any form containing `<button data-discount-panel>` gets this panel.
   The item list is read from the form's own line inputs, so it always matches
   what will be submitted.
   ========================================================================= */
(function () {
    "use strict";

    var MODAL_ID = "discountPanelModal";

    function money(value) {
        var n = parseFloat(value);
        if (!isFinite(n)) n = 0;
        return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    function toNumber(value) {
        var n = parseFloat(String(value == null ? "" : value).replace(/,/g, ""));
        return isFinite(n) ? n : 0;
    }

    function round2(value) {
        return Math.round((toNumber(value) + Number.EPSILON) * 100) / 100;
    }

    /* ---------------------------------------------------------------- lines */
    function readLines(form) {
        var names = form.querySelectorAll('[name="product_name[]"], [name="material_name[]"]');
        var qtys = form.querySelectorAll('[name="qty[]"]');
        var rates = form.querySelectorAll('[name="unit_rate[]"]');
        var byName = {};
        var order = [];

        Array.prototype.forEach.call(names, function (input, index) {
            var name = String(input.value || "").trim();
            if (!name) return;
            var qty = toNumber(qtys[index] ? qtys[index].value : 0);
            var rate = toNumber(rates[index] ? rates[index].value : 0);
            if (qty <= 0) return;
            var existing = byName[name];
            if (!existing) {
                existing = byName[name] = { name: name, qty: 0, gross: 0, rows: 0 };
                order.push(name);
            }
            existing.qty += qty;
            existing.gross += qty * rate;
            existing.rows += 1;
        });

        return order.map(function (name) {
            var line = byName[name];
            line.gross = round2(line.gross);
            line.rate = line.qty > 0 ? round2(line.gross / line.qty) : 0;
            return line;
        });
    }

    function isServiceName(name) {
        var text = String(name || "").toLowerCase();
        if (text.indexOf("rent") !== -1) return true;
        var marker = document.body.getAttribute("data-service-names") || "";
        if (!marker) return false;
        return marker.toLowerCase().split("|").indexOf(text) !== -1;
    }

    /* ---------------------------------------------------------------- modal */
    function buildModal() {
        var existing = document.getElementById(MODAL_ID);
        if (existing) return existing;

        var modal = document.createElement("div");
        modal.className = "modal fade";
        modal.id = MODAL_ID;
        modal.tabIndex = -1;
        modal.setAttribute("aria-hidden", "true");
        modal.innerHTML =
            '<div class="modal-dialog modal-lg modal-dialog-scrollable">' +
            '<div class="modal-content">' +
            '<div class="modal-header">' +
            '<h5 class="modal-title"><i class="bi bi-percent me-2"></i>Discount by item</h5>' +
            '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>' +
            '</div>' +
            '<div class="modal-body">' +
            '<p class="small text-muted mb-3">Tick the items that get a discount and type the amount for each. ' +
            'Unticked items keep their full price. Services such as rent are unticked by default because they ' +
            'are sold at a fixed rate.</p>' +
            '<div class="table-responsive"><table class="table table-sm align-middle">' +
            '<thead><tr><th style="width:2rem"></th><th>Item</th><th class="text-end">Qty</th>' +
            '<th class="text-end">Rate</th><th class="text-end">Gross</th>' +
            '<th class="text-end" style="width:9rem">Discount (Rs.)</th>' +
            '<th class="text-end" style="width:6rem">%</th></tr></thead>' +
            '<tbody data-discount-rows></tbody></table></div>' +
            '<div class="row g-2 align-items-end mt-3">' +
            '<div class="col-sm-5"><label class="form-label small mb-1">Discount all ticked items by one total</label>' +
            '<input type="number" step="0.01" min="0" class="form-control form-control-sm" data-discount-total></div>' +
            '<div class="col-sm-4"><label class="form-label small mb-1">Split</label>' +
            '<select class="form-select form-select-sm" data-discount-split>' +
            '<option value="value">By item value</option>' +
            '<option value="equal">Equally</option>' +
            '<option value="largest">To the largest item</option>' +
            '</select></div>' +
            '<div class="col-sm-3"><button type="button" class="btn btn-sm btn-outline-warning w-100" data-discount-distribute>Distribute</button></div>' +
            '</div>' +
            '</div>' +
            '<div class="modal-footer justify-content-between">' +
            '<div class="small"><span class="text-muted">Gross</span> <strong data-discount-gross>0.00</strong>' +
            ' &middot; <span class="text-muted">Discount</span> <strong data-discount-total-view>0.00</strong>' +
            ' &middot; <span class="text-muted">Net</span> <strong data-discount-net>0.00</strong></div>' +
            '<div>' +
            '<button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button> ' +
            '<button type="button" class="btn btn-warning" data-discount-apply>Apply discount</button>' +
            '</div></div>' +
            '</div></div>';
        document.body.appendChild(modal);
        return modal;
    }

    /* ------------------------------------------------------------ behaviour */
    function openPanel(form) {
        var modalEl = buildModal();
        var tbody = modalEl.querySelector("[data-discount-rows]");
        var grossView = modalEl.querySelector("[data-discount-gross]");
        var totalView = modalEl.querySelector("[data-discount-total-view]");
        var netView = modalEl.querySelector("[data-discount-net]");
        var lines = readLines(form);

        if (!lines.length) {
            window.alert("Add at least one item with a quantity before choosing a discount.");
            return;
        }

        var existing = {};
        Array.prototype.forEach.call(
            form.querySelectorAll('[name="item_discount_name[]"]'),
            function (input, index) {
                var values = form.querySelectorAll('[name="item_discount_value[]"]');
                existing[String(input.value || "").trim()] = toNumber(values[index] ? values[index].value : 0);
            }
        );

        tbody.innerHTML = "";
        lines.forEach(function (line) {
            var service = isServiceName(line.name);
            var already = existing[line.name] || 0;
            var checked = already > 0;
            var tr = document.createElement("tr");
            tr.setAttribute("data-item", line.name);
            tr.setAttribute("data-gross", String(line.gross));
            tr.setAttribute("data-service", service ? "1" : "0");
            tr.innerHTML =
                '<td><input class="form-check-input" type="checkbox" data-pick' + (checked ? " checked" : "") + '></td>' +
                '<td><div class="fw-bold">' + escapeHtml(line.name) + '</div>' +
                (service ? '<span class="badge text-bg-secondary">service</span>' : '') + '</td>' +
                '<td class="text-end">' + money(line.qty) + '</td>' +
                '<td class="text-end">' + money(line.rate) + '</td>' +
                '<td class="text-end">' + money(line.gross) + '</td>' +
                '<td class="text-end"><input type="number" step="0.01" min="0" class="form-control form-control-sm text-end" data-amount value="' +
                (checked ? String(already) : "") + '"' + (checked ? "" : " disabled") + '></td>' +
                '<td class="text-end"><input type="number" step="0.01" min="0" max="100" class="form-control form-control-sm text-end" data-percent' +
                (checked ? "" : " disabled") + '></td>';
            tbody.appendChild(tr);
        });

        function rowState(tr) {
            var pick = tr.querySelector("[data-pick]");
            var amount = tr.querySelector("[data-amount]");
            var percent = tr.querySelector("[data-percent]");
            amount.disabled = !pick.checked;
            percent.disabled = !pick.checked;
            return { pick: pick, amount: amount, percent: percent };
        }

        function refresh() {
            var gross = 0;
            var discount = 0;
            Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
                gross += toNumber(tr.getAttribute("data-gross"));
                var state = rowState(tr);
                if (state.pick.checked) {
                    var limit = toNumber(tr.getAttribute("data-gross"));
                    var value = toNumber(state.amount.value);
                    if (value > limit) {
                        value = limit;
                        state.amount.value = limit.toFixed(2);
                    }
                    discount += Math.max(0, value);
                    var pct = limit > 0 ? round2((Math.max(0, value) / limit) * 100) : 0;
                    if (document.activeElement !== state.percent) {
                        state.percent.value = pct ? String(pct) : "";
                    }
                } else {
                    state.amount.value = "";
                    state.percent.value = "";
                }
            });
            gross = round2(gross);
            discount = round2(discount);
            grossView.textContent = money(gross);
            totalView.textContent = money(discount);
            netView.textContent = money(round2(gross - discount));
        }

        Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
            var state = rowState(tr);
            state.pick.addEventListener("change", function () {
                if (state.pick.checked && !toNumber(state.amount.value)) {
                    state.amount.focus();
                }
                refresh();
            });
            state.amount.addEventListener("input", refresh);
            state.percent.addEventListener("input", function () {
                var limit = toNumber(tr.getAttribute("data-gross"));
                state.amount.value = round2(limit * toNumber(state.percent.value) / 100).toFixed(2);
                refresh();
            });
        });

        var totalInput = modalEl.querySelector("[data-discount-total]");
        var splitSelect = modalEl.querySelector("[data-discount-split]");
        modalEl.querySelector("[data-discount-distribute]").onclick = function () {
            var pool = round2(toNumber(totalInput.value));
            var rows = Array.prototype.filter.call(
                tbody.querySelectorAll("tr"),
                function (tr) { return tr.querySelector("[data-pick]").checked; }
            );
            if (!rows.length || pool <= 0) {
                window.alert("Tick the items first, then enter the total to distribute.");
                return;
            }
            var grosses = rows.map(function (tr) { return toNumber(tr.getAttribute("data-gross")); });
            var weights = grosses.slice();
            if (splitSelect.value === "equal") {
                weights = grosses.map(function () { return 1; });
            } else if (splitSelect.value === "largest") {
                var maxIndex = grosses.indexOf(Math.max.apply(null, grosses));
                weights = grosses.map(function (_g, index) { return index === maxIndex ? 1 : 0; });
            }
            var totalWeight = weights.reduce(function (a, b) { return a + b; }, 0) || 1;
            // Largest-remainder split in paisa, mirroring app.services.pricing so
            // the panel and the server can never disagree.
            var poolCents = Math.round(pool * 100);
            var exact = weights.map(function (weight) { return poolCents * weight / totalWeight; });
            var floors = exact.map(function (value) { return Math.floor(value); });
            var remainder = poolCents - floors.reduce(function (a, b) { return a + b; }, 0);
            var order = exact
                .map(function (value, index) { return { index: index, frac: value - Math.floor(value) }; })
                .sort(function (a, b) { return (b.frac - a.frac) || (weights[b.index] - weights[a.index]); });
            for (var i = 0; i < remainder && i < order.length; i += 1) {
                floors[order[i].index] += 1;
            }
            rows.forEach(function (tr, index) {
                tr.querySelector("[data-amount]").value = (floors[index] / 100).toFixed(2);
            });
            refresh();
        };

        modalEl.querySelector("[data-discount-apply]").onclick = function () {
            var names = [];
            var values = [];
            var headerTotal = 0;
            Array.prototype.forEach.call(tbody.querySelectorAll("tr"), function (tr) {
                var state = rowState(tr);
                if (!state.pick.checked) return;
                var amount = round2(toNumber(state.amount.value));
                if (amount <= 0) return;
                names.push(tr.getAttribute("data-item"));
                values.push(amount.toFixed(2));
                headerTotal += amount;
            });

            form.querySelectorAll('[name="item_discount_name[]"], [name="item_discount_value[]"]').forEach(function (node) {
                node.parentNode.removeChild(node);
            });
            names.forEach(function (name, index) {
                var nameInput = document.createElement("input");
                nameInput.type = "hidden";
                nameInput.name = "item_discount_name[]";
                nameInput.value = name;
                form.appendChild(nameInput);
                var valueInput = document.createElement("input");
                valueInput.type = "hidden";
                valueInput.name = "item_discount_value[]";
                valueInput.value = values[index];
                form.appendChild(valueInput);
            });

            var headerDiscount = form.querySelector('input[name="discount"]');
            if (headerDiscount) {
                headerDiscount.value = round2(headerTotal).toFixed(2);
                headerDiscount.dispatchEvent(new Event("input", { bubbles: true }));
                headerDiscount.dispatchEvent(new Event("change", { bubbles: true }));
            }

            var summary = form.querySelector("[data-discount-summary]");
            if (summary) {
                if (names.length) {
                    summary.innerHTML = '<span class="badge text-bg-warning">' +
                        names.length + ' item' + (names.length === 1 ? "" : "s") +
                        " discounted &middot; Rs. " + money(round2(headerTotal)) + "</span>";
                } else {
                    summary.innerHTML = "";
                }
            }

            var instance = window.bootstrap && window.bootstrap.Modal.getInstance(modalEl);
            if (instance) instance.hide();
        };

        refresh();
        var modal = window.bootstrap
            ? (window.bootstrap.Modal.getInstance(modalEl) || new window.bootstrap.Modal(modalEl))
            : null;
        if (modal) {
            modal.show();
        } else {
            modalEl.classList.add("show");
            modalEl.style.display = "block";
        }
    }

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    document.addEventListener("click", function (event) {
        var button = event.target.closest("[data-discount-panel]");
        if (!button) return;
        event.preventDefault();
        var form = button.closest("form");
        if (form) openPanel(form);
    });
})();
