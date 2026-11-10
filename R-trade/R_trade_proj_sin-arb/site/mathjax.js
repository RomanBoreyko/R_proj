window.MathJax = {
  loader: { load: ["[tex]/unicode"] },   // \unicode{36} = «$» в формулах (build_src.py) — грузим заранее, без autoload-retry
  tex: {
    packages: { "[+]": ["unicode"] },
    inlineMath: [["\(", "\)"]], displayMath: [["\[", "\]"]], processEscapes: true, processEnvironments: true
  },
  options: { ignoreHtmlClass: ".*|", processHtmlClass: "arithmatex" }
};
// Material (instant navigation): перерисовать формулы на каждой странице.
// На первой загрузке MathJax ещё не готов — тогда страницу отрисует сам startup.
document$.subscribe(() => {
  const mj = window.MathJax;
  if (!mj || !mj.startup || !mj.startup.output || !mj.typesetPromise) return;
  mj.startup.output.clearCache();
  mj.typesetClear();
  mj.texReset();
  mj.typesetPromise();
});
