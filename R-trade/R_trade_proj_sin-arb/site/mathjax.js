window.MathJax = {
  loader: { load: ["[tex]/unicode"] },   // \unicode{36} = «$» в формулах (build_src.py) — грузим заранее, без autoload-retry
  tex: {
    packages: { "[+]": ["unicode"] },
    inlineMath: [["\(", "\)"]], displayMath: [["\[", "\]"]], processEscapes: true, processEnvironments: true
  },
  options: { ignoreHtmlClass: ".*|", processHtmlClass: "arithmatex" }
};
document$.subscribe(() => {
  MathJax.startup.output.clearCache();
  MathJax.typesetClear();
  MathJax.texReset();
  MathJax.typesetPromise();
});
