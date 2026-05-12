// Public Momentum directory bootstrap. Runs on the GFS landing
// at ``/moments``; renders cards from ``GET /gfs/users`` and
// supports substring search. Vanilla JS — no Preact — to keep
// the public surface tiny.
"use strict";

(function () {
  const list = document.getElementById("dir-list");
  const empty = document.getElementById("dir-empty");
  const search = document.getElementById("dir-search");
  if (!list || !search || !empty) return;

  let users = [];

  function escape(s) {
    return String(s ?? "").replace(/[&<>"']/g, function (c) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[c];
    });
  }

  function avatarSrc(u) {
    if (u.picture_digest) {
      return (
        "/gfs/users/" +
        encodeURIComponent(u.user_id) +
        "/picture?v=" +
        encodeURIComponent(u.picture_digest)
      );
    }
    return "/static/avatar_placeholder.svg";
  }

  function render(filtered) {
    if (filtered.length === 0) {
      list.innerHTML = "";
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    list.innerHTML = filtered
      .map(function (u) {
        return (
          '<li class="card"><a href="/moments/' +
          encodeURIComponent(u.user_id) +
          '">' +
          '<img class="avatar" alt="" loading="lazy" src="' +
          escape(avatarSrc(u)) +
          '">' +
          '<div class="who"><strong>' +
          escape(u.display_name) +
          "</strong>" +
          '<small>@' +
          escape(u.username) +
          " · " +
          escape(u.instance_id) +
          "</small>" +
          (u.bio
            ? '<p class="bio">' + escape(u.bio) + "</p>"
            : "") +
          "</div></a></li>"
        );
      })
      .join("");
  }

  function applyFilter() {
    const q = search.value.trim().toLowerCase();
    if (!q) {
      render(users);
      return;
    }
    render(
      users.filter(function (u) {
        return (
          (u.display_name || "").toLowerCase().includes(q) ||
          (u.username || "").toLowerCase().includes(q) ||
          (u.bio || "").toLowerCase().includes(q)
        );
      }),
    );
  }

  fetch("/gfs/users")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      users = (data && data.users) || [];
      render(users);
    })
    .catch(function () {
      list.innerHTML = "";
      empty.hidden = false;
      empty.textContent = "Couldn't load directory.";
    });

  search.addEventListener("input", applyFilter);
})();
