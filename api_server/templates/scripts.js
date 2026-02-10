// /static/js/scripts.js

(function() {
    // Function to ensure data-theme exists and fallback to cookie
    function ensureTheme() {
        try {
            const allowed = new Set(['paper', 'dark', 'light']);
            const body = document.body;
            const current = body.getAttribute('data-theme');
            if (!current || !allowed.has(current)) {
                const match = document.cookie.match(/(?:^|; )theme=([^;]+)/);
                const theme = match ? decodeURIComponent(match[1]) : null;
                const apply = allowed.has(theme) ? theme : 'paper';
                body.setAttribute('data-theme', apply);
            }
        } catch (e) { console.warn('theme apply failed', e); }
    }

    // Font selection persistence
    function handleFontSelection() {
        const fontSelect = document.getElementById('font-select');
        const content = document.getElementById('content');
        if (fontSelect && content) {
            const saved = localStorage.getItem('reader_font');
            if (saved) { fontSelect.value = saved; content.style.fontFamily = saved; }
            fontSelect.addEventListener('change', function() {
                const f = fontSelect.value || '';
                content.style.fontFamily = f;
                localStorage.setItem('reader_font', f);
            });
        }
    }

    // Spacing toggle
    function handleSpacingToggle() {
        const spacingBtn = document.getElementById('spacing-toggle');
        if (spacingBtn) {
            spacingBtn.addEventListener('click', function() {
                const params = new URLSearchParams(window.location.search);
                const current = params.get('spacing') === '1';
                if (current) params.delete('spacing');
                else params.set('spacing', '1');
                window.location.search = params.toString();
            });
        }
    }

    // Next button behavior (toast then redirect if last chapter)
    function handleNextButton() {
        const btn = document.getElementById('next-btn');
        if (!btn) return;
        btn.addEventListener('click', function(e) {
            if (btn.getAttribute('data-no-next') === '1') {
                e.preventDefault();
                const toast = document.getElementById('toast');
                if (toast) { toast.classList.add('show'); }
                setTimeout(function() {
                    if (toast) { toast.classList.remove('show'); }
                    window.location.href = '/web/novel/{{ novel_id }}';
                }, 1100);
            }
        });
    }

    // View mode buttons (original / combined / translation)
    function handleViewModeButtons() {
        const btnOrig = document.getElementById('view-original');
        const btnComb = document.getElementById('view-combined');
        const btnTrans = document.getElementById('view-translation');
        const transSec = document.getElementById('trans-section');
        const origSec = document.getElementById('content');

        function applyMode(mode) {
            localStorage.setItem('reader_view_mode', mode);
            if(btnOrig) btnOrig.classList.remove('primary');
            if(btnComb) btnComb.classList.remove('primary');
            if(btnTrans) btnTrans.classList.remove('primary');

            if(mode === 'original' || !transSec){
                if(origSec) origSec.style.display = 'block';
                if(transSec) transSec.style.display = 'none';
                if(btnOrig) btnOrig.classList.add('primary');
            } else if(mode === 'translation'){
                if(origSec) origSec.style.display = 'none';
                if(transSec) transSec.style.display = 'block';
                if(btnTrans) btnTrans.classList.add('primary');
            } else {
                if(origSec) origSec.style.display = 'block';
                if(transSec) transSec.style.display = 'block';
                if(btnComb) btnComb.classList.add('primary');
            }
        }

        if(btnOrig) btnOrig.addEventListener('click', () => applyMode('original'));
        if(btnComb) btnComb.addEventListener('click', () => applyMode('combined'));
        if(btnTrans) btnTrans.addEventListener('click', () => applyMode('translation'));

        try{
            const saved = localStorage.getItem('reader_view_mode') || 'combined';
            if(!transSec) saved = 'original';
            applyMode(saved);
        }catch(e){ applyMode('combined'); }
    }

    // Tabs for request.html
    function handleTabs() {
        const tabs = document.querySelectorAll(".tab");
        const urlInput = document.getElementById("url");
        const siteHint = document.getElementById("siteHint");
        const exampleHint = document.getElementById("exampleHint");

        function setActive(site) {
            tabs.forEach(function (t) {
                var isActive = t.getAttribute("data-site") === site;
                t.classList.toggle("active", isActive);
                t.setAttribute("aria-selected", isActive ? "true" : "false");
            });

            if (site === "syosetu") {
                urlInput.placeholder = "예: https://ncode.syosetu.com/n3289ds/1/";
                siteHint.textContent = "지원 사이트: 소설가가되자";
                exampleHint.textContent = "예시: 제목 `테스트소설`, URL `https://ncode.syosetu.com/n3289ds/1/`";
            } else {
                urlInput.placeholder = "예: https://booktoki469.com/novel/20661854";
                siteHint.textContent = "지원 사이트: 북토끼";
                exampleHint.textContent = "예시: 제목 `테스트소설`, URL `https://booktoki469.com/novel/20661854`";
            }
        }

        tabs.forEach(function (t) {
            t.addEventListener("click", function () {
                setActive(t.getAttribute("data-site"));
            });
        });

        // Initial setup
        setActive('booktoki');
    }

    // Run all functions on document ready
    document.addEventListener('DOMContentLoaded', function() {
        ensureTheme();
        handleFontSelection();
        handleSpacingToggle();
        handleNextButton();
        handleViewModeButtons();
        handleTabs();
    });
})();