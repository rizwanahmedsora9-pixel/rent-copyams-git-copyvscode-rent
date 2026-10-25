/* =========================================================================
   Profit & Loss — automatic filters and searchable dropdowns.

   Responsibilities
   ----------------
   * Every filter change refreshes the report immediately (no Apply button).
     Free-text fields are debounced; selects and dates apply at once.
   * Only the results region is re-rendered (`?partial=1`), so the filters keep
     their focus, scroll position and selected values.
   * While a request is in flight the results are dimmed and a progress bar is
     shown, so stale figures are never presented as current.
   * All arithmetic stays on the server.  This script never computes a total.

   Loaded only by the P&L page; no dependency on any other module.
   ========================================================================= */
(function () {
    "use strict";

    var DEBOUNCE_MS = 500;

    function each(root, selector, fn) {
        (root || document).querySelectorAll(selector).forEach(fn);
    }

    function normalise(value) {
        return String(value == null ? "" : value).trim().toLowerCase();
    }

    /* ---------------------------------------------------------------- combobox */
    function Combo(wrapper, config) {
        this.wrapper = wrapper;
        this.config = config || {};
        this.options = [];
        this.activeIndex = -1;
        if (!wrapper) {
            // The page rendered without this control; stay inert instead of
            // throwing and taking the whole filter bar down with us.
            this.input = null;
            this.menu = null;
            this.hidden = null;
            this.categoryHidden = null;
            return;
        }
        this.role = wrapper.getAttribute("data-combo");
        this.input = wrapper.querySelector("[data-combo-input]");
        this.menu = wrapper.querySelector("[data-combo-menu]");
        this.hidden = wrapper.querySelector("[data-combo-value]");
        this.categoryHidden = wrapper.querySelector("[data-combo-category]");
        if (!this.input || !this.menu) return;
        this.bind();
        this.refreshOptions();
    }

    Combo.prototype.entityType = function () {
        var select = document.getElementById("plEntityType");
        return select ? select.value : "";
    };

    Combo.prototype.isCategoryMode = function () {
        return this.role === "client" && this.entityType() === "category";
    };

    Combo.prototype.refreshOptions = function () {
        if (!this.wrapper) return;
        var data = this.config.data || {};
        if (this.role === "material") {
            this.options = (data.materials || []).map(function (name) {
                return { value: name, hint: "" };
            });
        } else if (this.isCategoryMode()) {
            this.options = (data.categories || []).map(function (name) {
                return { value: name, hint: "category" };
            });
        } else {
            var names = data.customers || [];
            var codes = data.customerCodes || [];
            this.options = names.map(function (name, index) {
                return { value: name, hint: codes[index] || "" };
            });
        }
    };

    Combo.prototype.matches = function (query) {
        var needle = normalise(query);
        if (!needle) return this.options.slice(0, 200);
        var out = [];
        for (var i = 0; i < this.options.length && out.length < 200; i += 1) {
            var option = this.options[i];
            if (normalise(option.value).indexOf(needle) >= 0 || normalise(option.hint).indexOf(needle) >= 0) {
                out.push(option);
            }
        }
        return out;
    };

    Combo.prototype.renderMenu = function (query) {
        var menu = this.menu;
        if (!menu || !this.wrapper) return;
        var list = this.matches(query);
        menu.innerHTML = "";
        if (!list.length) {
            var empty = document.createElement("div");
            empty.className = "pl-combo-empty";
            empty.textContent = "No matches";
            menu.appendChild(empty);
            menu.hidden = false;
            this.setExpanded(true);
            return;
        }
        var self = this;
        list.forEach(function (option, index) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "pl-combo-option" + (index === self.activeIndex ? " is-active" : "");
            button.setAttribute("data-value", option.value);
            button.setAttribute("role", "option");
            button.textContent = option.value;
            if (option.hint) {
                var hint = document.createElement("span");
                hint.className = "pl-combo-hint";
                hint.textContent = option.hint;
                button.appendChild(hint);
            }
            button.addEventListener("mousedown", function (ev) {
                ev.preventDefault();
                self.select(option.value);
            });
            menu.appendChild(button);
        });
        menu.hidden = false;
        this.setExpanded(true);
    };

    Combo.prototype.select = function (value) {
        if (!this.wrapper) return;
        this.input.value = value || "";
        if (this.isCategoryMode()) {
            if (this.categoryHidden) this.categoryHidden.value = value || "";
            if (this.hidden) this.hidden.value = "";
        } else {
            if (this.hidden) this.hidden.value = value || "";
            if (this.categoryHidden) this.categoryHidden.value = "";
        }
        this.close();
        this.config.onCommit && this.config.onCommit();
    };

    Combo.prototype.setExpanded = function (open) {
        if (this.input) this.input.setAttribute("aria-expanded", open ? "true" : "false");
    };

    Combo.prototype.close = function () {
        this.activeIndex = -1;
        if (this.menu) this.menu.hidden = true;
        this.setExpanded(false);
    };

    Combo.prototype.bind = function () {
        var self = this;
        this.input.addEventListener("focus", function () {
            self.renderMenu(self.input.value);
        });
        this.input.addEventListener("input", function () {
            self.activeIndex = -1;
            self.renderMenu(self.input.value);
            self.config.onType && self.config.onType(self.input.value);
        });
        this.input.addEventListener("blur", function () {
            // Selecting by click fires mousedown first; delay closing so the
            // click is not lost, then fall back to the typed text.
            window.setTimeout(function () {
                self.close();
                self.config.onBlur && self.config.onBlur(self.input.value);
            }, 140);
        });
        this.input.addEventListener("keydown", function (ev) {
            var list = self.menu ? self.menu.querySelectorAll(".pl-combo-option") : [];
            if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
                if (!list.length) return;
                ev.preventDefault();
                self.activeIndex += ev.key === "ArrowDown" ? 1 : -1;
                if (self.activeIndex < 0) self.activeIndex = list.length - 1;
                if (self.activeIndex >= list.length) self.activeIndex = 0;
                Array.prototype.forEach.call(list, function (el, index) {
                    el.classList.toggle("is-active", index === self.activeIndex);
                });
            } else if (ev.key === "Enter") {
                if (self.activeIndex >= 0 && list[self.activeIndex]) {
                    ev.preventDefault();
                    self.select(list[self.activeIndex].getAttribute("data-value"));
                }
            } else if (ev.key === "Escape") {
                self.close();
            }
        });
        document.addEventListener("click", function (ev) {
            if (!self.wrapper.contains(ev.target)) self.close();
        });
    };

    /* ------------------------------------------------------------ controller */
    function init(config) {
        var form = document.querySelector(config.form || "#plFilterForm");
        var region = document.querySelector(config.region || "#plResultsRegion");
        if (!form || !region) return;

        var loadingBar = document.querySelector(config.loadingBar || "#plLoadingBar");
        var stateLabel = document.querySelector(config.stateLabel || "#plFilterState");
        var data = config.data || {};
        var controller = null;
        var timer = null;
        var lastUrl = window.location.href;

        function setBusy(busy) {
            var surface = region.querySelector("[data-pl-results]") || region;
            surface.classList.toggle("is-loading", busy);
            if (loadingBar) loadingBar.classList.toggle("is-on", busy);
            if (stateLabel) {
                stateLabel.classList.toggle("is-busy", busy);
                stateLabel.innerHTML = busy
                    ? '<i class="bi bi-arrow-repeat"></i> Updating…'
                    : '<i class="bi bi-cloud-check"></i> Filters apply automatically';
            }
        }

        function partialName() {
            return config.partialParam || "partial";
        }

        function absolute(value) {
            try {
                return new URL(value, window.location.href);
            } catch (error) {
                return null;
            }
        }

        /* Link targets are ordinary bookmarks: they carry no ``partial`` flag.
           The fetch needs it, the address bar must not. */
        function splitPartial(url) {
            var parsed = absolute(url);
            if (!parsed) return { fetchUrl: url, cleanUrl: url };
            var clean = new URL(parsed.toString());
            clean.searchParams.delete(partialName());
            return {
                fetchUrl: parsed.toString() + (parsed.search ? "&" : "?") + partialName() + "=1",
                cleanUrl: clean.pathname + (clean.search || "")
            };
        }

        function buildUrl(overrides) {
            var params = new URLSearchParams();
            each(form, "input, select", function (field) {
                if (!field.name) return;
                if (field.disabled) return;
                var type = (field.type || "").toLowerCase();
                if ((type === "checkbox" || type === "radio") && !field.checked) return;
                var value = String(field.value == null ? "" : field.value).trim();
                if (!value) return;
                params.set(field.name, value);
            });
            if (overrides) {
                Object.keys(overrides).forEach(function (key) {
                    if (overrides[key] === null || overrides[key] === "") params.delete(key);
                    else params.set(key, overrides[key]);
                });
            }
            params.set(partialName(), "1");
            return form.getAttribute("action") + "?" + params.toString();
        }

        /* The server resolves presets ("this month") into concrete dates.  Show
           them in the date fields so a later manual edit continues from the
           range the reader is actually looking at. */
        function syncDateInputs() {
            var surface = region.querySelector("[data-pl-results]");
            if (!surface) return;
            [["start_date", "data-pl-start"], ["end_date", "data-pl-end"]].forEach(function (pair) {
                var field = form.querySelector('[name="' + pair[0] + '"]');
                if (!field) return;
                var value = surface.getAttribute(pair[1]) || "";
                if (field.value !== value) field.value = value;
            });
        }

        function clearError() {
            var existing = region.querySelector(".pl-error");
            if (existing) existing.remove();
        }

        function showError(message) {
            clearError();
            var box = document.createElement("div");
            box.className = "pl-error";
            box.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i> ' + message;
            region.insertBefore(box, region.firstChild);
        }

        function load(url, options) {
            options = options || {};
            if (controller) controller.abort();
            controller = window.AbortController ? new AbortController() : null;
            setBusy(true);
            clearError();
            var target = splitPartial(url);
            fetch(target.fetchUrl, {
                headers: { "X-Requested-With": "fetch" },
                credentials: "same-origin",
                signal: controller ? controller.signal : undefined
            }).then(function (response) {
                if (!response.ok) throw new Error("The report could not be generated (HTTP " + response.status + ").");
                return response.text();
            }).then(function (html) {
                region.innerHTML = html;
                syncDateInputs();
                var cleanUrl = target.cleanUrl;
                if (!options.keepScroll) {
                    // Keep the reader anchored to the results they were reading.
                    var top = region.getBoundingClientRect().top + window.pageYOffset - 12;
                    if (window.pageYOffset > top) window.scrollTo({ top: top, behavior: "auto" });
                }
                lastUrl = cleanUrl;
                window.history.replaceState({}, "", cleanUrl);
                setBusy(false);
                region.dispatchEvent(new CustomEvent("pl:updated"));
            }).catch(function (error) {
                if (error && error.name === "AbortError") return;
                setBusy(false);
                showError((error && error.message ? error.message : "The report could not be updated.") + " <a href=\"" + splitPartial(url).cleanUrl + "\">Open the full page</a>.");
            });
        }

        function refresh(overrides) {
            window.clearTimeout(timer);
            load(buildUrl(overrides));
        }

        function refreshDebounced(overrides) {
            window.clearTimeout(timer);
            timer = window.setTimeout(function () { refresh(overrides); }, DEBOUNCE_MS);
        }

        /* --- filters ------------------------------------------------------ */
        /* A preset period owns the dates; editing a date therefore means an
           explicit custom range, otherwise the typed dates would be ignored. */
        function markCustomPeriod() {
            var periodSelect = form.querySelector('[name="period"]');
            if (!periodSelect) return;
            if (periodSelect.value === "custom") return;
            var option = periodSelect.querySelector('option[value="custom"]');
            if (!option) return;
            periodSelect.value = "custom";
        }

        each(form, "[data-pl-auto]", function (field) {
            field.addEventListener("change", function () {
                if (field.name === "entity_type") comboClient.refreshOptions();
                if (field.name === "start_date" || field.name === "end_date") {
                    markCustomPeriod();
                } else if (field.name === "period" && field.value !== "custom") {
                    // Presets recompute both dates on the server; clear the
                    // stale values so they are not submitted with the preset.
                    var start = form.querySelector('[name="start_date"]');
                    var end = form.querySelector('[name="end_date"]');
                    if (start) start.value = "";
                    if (end) end.value = "";
                }
                refresh();
            });
        });
        each(form, "[data-pl-debounce]", function (field) {
            field.addEventListener("input", function () { refreshDebounced(); });
            field.addEventListener("change", function () { refresh(); });
        });

        var comboClient = new Combo(form.querySelector('[data-combo="client"]'), {
            data: data,
            onCommit: function () { refresh(); },
            onType: function (value) {
                // In customer mode the typed text is itself a valid filter
                // (matched by name or code), so apply it after a pause.
                if (comboClient.isCategoryMode()) return;
                comboClient.hidden.value = value || "";
                comboClient.categoryHidden.value = "";
                refreshDebounced();
            },
            onBlur: function (value) {
                if (comboClient.isCategoryMode()) {
                    // Category filtering is exact: only commit a real category.
                    var known = (data.categories || []).some(function (name) {
                        return normalise(name) === normalise(value);
                    });
                    if (!known) {
                        comboClient.input.value = comboClient.categoryHidden.value || "";
                        return;
                    }
                    comboClient.select(value);
                    return;
                }
                comboClient.hidden.value = value || "";
                refresh();
            }
        });

        var comboMaterial = new Combo(form.querySelector('[data-combo="material"]'), {
            data: data,
            onCommit: function () { refresh(); },
            onType: function (value) {
                comboMaterial.hidden.value = value || "";
                refreshDebounced();
            },
            onBlur: function (value) {
                comboMaterial.hidden.value = value || "";
                refresh();
            }
        });

        var entitySelect = document.getElementById("plEntityType");
        if (entitySelect) {
            entitySelect.addEventListener("change", function () {
                var label = form.querySelector("[data-pl-entity-label]");
                if (label) label.textContent = entitySelect.value === "category" ? "Client category" : "Customer / company site";
                if (comboClient) {
                    comboClient.hidden.value = "";
                    comboClient.categoryHidden.value = "";
                    comboClient.input.value = "";
                    comboClient.input.placeholder = entitySelect.value === "category"
                        ? "All categories" : "All customers — type to search";
                    comboClient.refreshOptions();
                }
            });
        }

        form.addEventListener("submit", function (ev) {
            ev.preventDefault();
            refresh();
        });

        /* --- navigation inside the results region ------------------------- */
        region.addEventListener("click", function (ev) {
            var link = ev.target.closest("a[data-pl-nav]");
            if (!link) return;
            var href = link.getAttribute("href");
            if (!href || href === "#") {
                ev.preventDefault();
                return;
            }
            ev.preventDefault();
            load(href);
        });

        window.addEventListener("popstate", function () {
            window.location.reload();
        });

        setBusy(false);
        if (lastUrl) window.history.replaceState({}, "", lastUrl);
    }

    window.FBMProfitFilters = { init: init };
})();
