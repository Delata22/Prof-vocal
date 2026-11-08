# Moteur de scènes : référence

API calquée sur manim (3Blue1Brown), en JavaScript. Une carte = un fichier `cards/<id>.js` :

```js
scene({h: 7}, s => {
  // objets et mise en page ; s.add(…) = visible dès le début
  s.step("Légende de l'étape 0 (une phrase, $\\LaTeX$ permis).", () => { s.play(…); s.play(…); });
  s.step("Étape 1.", () => { … });
});
```

- **Cadre** : `w` × `h` unités (défaut 16 × 9), origine au centre, y vers le haut. Choisis `h` pour que le dessin remplisse le cadre (souvent 6 à 8).
- **Étapes** : chaque `s.step` est un pas ; les `s.play(…)` d'une étape s'enchaînent. La page a ◀ ▶ ↻ et ta voix pilote avec `⟦step <id> k⟧` (k = 0 pour la première étape). Un objet créé hors des étapes et passé à `s.add` est visible d'emblée ; un objet créé n'importe où n'apparaît qu'au premier `s.add`, `Create`, `Write` ou `FadeIn`.
- `new` est facultatif partout. Directions : `UP DOWN LEFT RIGHT UL UR DL DR ORIGIN` (vecteurs `[x, y]`). Opérations sur les points : `add(a, b)`, `sub(a, b)`, `mul(a, k)`.
- **Couleurs** : `BLUE TEAL GREEN YELLOW GOLD RED MAROON PURPLE PINK ORANGE GREY FG MUTED` (adaptées au thème clair ou sombre). Dans une formule : `\blue{x}`, `\red{…}`, `\green{…}`… (mêmes teintes que les objets).

## Objets

| Objet | Rôle |
|---|---|
| `Tex("\\frac{a}{b}", {size, color, at})` | formule (KaTeX, style display) ; `size` 1 = normal, 0.7 = petit |
| `Text("Texte avec $x^2$", {size, color, width})` | texte (maths entre `$`) ; `width` en unités = retour à la ligne |
| `Num(3.5, {digits: 1, color})` | nombre animable : `n.animate.set({value: 7})` compte de 3.5 à 7 |
| `Dot([x, y], {color, r})`, `Circle({r, at, color, fill, fop})`, `Rect({w, h, at, round, color, fill, fop})` | formes (`fill` : couleur de remplissage, `fop` : son opacité) |
| `Polygon(pts, o)`, `Polyline(pts, o)` | points en coordonnées du cadre |
| `Line(a, b, o)`, `Arrow(a, b, o)`, `DoubleArrow`, `DashedLine` | a et b : points, **objets** (la flèche part du bord et suit l'objet), ou fonctions `() => [x, y]` ; `{color, width, buff}` |
| `line.label(tex, {t, side, color, size})` | étiquette d'un segment, qui le suit (t = 0 départ, 1 arrivée) ; side = 0 : **sur** le trait, dans une pastille ; 1 ou -1 : à côté |
| `Node(tex, {r, color, name, at})` | neurone / sommet : cercle + valeur dedans (+ `name` au-dessus) ; `node.set_value("5", {color: RED})` est une animation |
| `Box(tex, {color, name, w, h, at})` | case arrondie + contenu ; `box.set_value(…)` |
| `Matrix([["1","2"],["3","4"]], {color, colors, at, rowLabels: ["u", "v"], colLabels: ["p", "q"], rowColors, colColors})` | matrice ; étiquettes de lignes (à droite) et de colonnes (au-dessus), alignées ; `m.entry(i, j)`, `m.row(i)`, `m.col(j)`, `m.entries()`, `m.brackets`, `m.labels` |
| `Table([[9, 9, 0]], {heat: GOLD, rowLabels, colLabels, size})` | grille de valeurs (pixels, noyau) ; `heat` colore selon la valeur ; `t.cell(i, j)`, `t.row(i)`, `t.col(j)` |
| `Brace(obj, DOWN, {label: "n", color})` | accolade sur un côté, avec étiquette |
| `SurroundingRect(obj, {color, buff})` | cadre autour d'un objet |
| `Group(a, b, …)` | groupe : `g.arrange(DOWN, 0.3)`, `g.arrange_grid(cols, buff)`, `g.at(i)` |
| `Axes({x: [min, max, pas], y: [...], w, h, at, xlabel, ylabel})` | repère gradué ; `ax.c2p(x, y)` → point du cadre |
| `ax.plot((x, k) => …, {k, color, x: [a, b]})` | courbe ; paramètre `k` animable : `g.animate.set({k: 2})` (famille de courbes) |
| `ax.area(f, a, b, {color})`, `ax.dot(x, y)`, `ax.vline(x, y)`, `ax.hline(x, y)` | aire sous la courbe, point, pointillés vers les axes |
| `Plane({x: [-7, 7], y: [-4, 4], unit, at})` | plan quadrillé déformable ; `pl.vector([1, 2], {color, label})`, `pl.basis()` → [î, ĵ], `pl.dot([x, y])`, `pl.c2p(x, y)` |
| `Network(layers, {w, h, at, r})` | réseau : `layers` = `[["x"], ["p", "q"], [{id: "y", tex: "\\hat y", color: GOLD, name: "sortie"}]]` ; arêtes entre couches voisines (ou `edges: [["x","p"], …]`) ; `net.node(id)`, `net.edge(a, b)`, `net.edges_from(id)`, `net.edges_to(id)`, `net.weight(a, b, "2")` (pastille posée sur l'arête, près du départ : sans ambiguïté même quand les arêtes se croisent) |

## Placement (chaînable : `Tex("x").next_to(c, UP, 0.2).shift([0.5, 0])`)

`move_to(point ou objet)`, `next_to(objet ou point, dir, buff=0.25)`, `align_to(objet, dir)`, `shift([dx, dy])`, `to_edge(dir, buff)`, `to_corner(UR, buff)`, `scale(k)`, `set_color(c)`, `set_opacity(o)`, `copy()`.
Mesures : `width`, `height`, `center()`, `get_top()`, `get_bottom()`, `get_left()`, `get_right()`.
Un objet qui doit en suivre un autre : `lab.add_updater(m => m.next_to(dot, UP))` (sur un objet simple, pas un groupe).
**Ne place jamais au jugé ce qui peut l'être relativement** (`next_to`, `arrange`, `label`) : c'est ce qui évite les chevauchements.

## Animations : `s.play(a, b, …, {time: 2})` les joue ensemble

| Animation | Effet |
|---|---|
| `Create(obj)`, `Write(tex)` | trace les contours / écrit de gauche à droite |
| `FadeIn(obj, {shift: [0, 0.3]})`, `FadeOut(obj)`, `Uncreate(obj)` | apparaît / disparaît |
| `obj.animate.shift(RIGHT).set_color(RED).scale(1.2)` | anime n'importe quelle suite de méthodes de placement |
| `Transform(a, b)` | a se déplace et se change en b (a disparaît, b reste) |
| `TransformFromCopy(a, b)` | **une copie de a vole jusqu'à b et devient b** : le symbole vient du dessin |
| `ChangeTex(tex, "5", {color})` | remplace un contenu sur place (l'ancien monte, le nouveau arrive) |
| `Indicate(obj, {color})` | grossit et colore brièvement : « regarde ça » |
| `Circumscribe(obj)` | trace un cadre autour, qui s'efface |
| `Flow(line, {color: RED, reverse: true, paint: true})` | un point lumineux parcourt un segment ou une courbe (propagation, rétropropagation) ; `paint` le laisse coloré |
| `ApplyMatrix(plane, [[a, b], [c, d]])` | déforme le plan et tout ce qui y est attaché (vecteurs, points) |
| `LaggedStart(a, b, c, {lag: 0.25})` | lance les animations en décalé |
| `s.wait(1)`, `s.do(() => …)` | pause ; changement instantané à ce moment de l'étape |

## Vérification

`python3 <dossier du prof>/scene.py add <slug> <id> "Titre" [--step "…"] --file cards/<id>.js` dessine chaque étape hors écran, et refuse la carte s'il y a une erreur JS, deux textes qui se chevauchent, un trait qui traverse un texte, un texte qui déborde d'une forme, ou un objet hors du cadre. Il écrit `cards/<id>.png` (toutes les étapes). `check` au lieu de `add` : vérifie sans ajouter. Les **conseils** (place mal utilisée, trop de texte) ne bloquent pas.

## Exemple : rétropropagation sur un petit réseau

```js
scene({h: 6}, s => {
  const net = Network([[{id: "x", color: GREEN}], ["p", "q"], [{id: "u", color: PURPLE}, {id: "v", color: PURPLE}], [{id: "y", tex: "\\bar y", color: GOLD}]],
                      {w: 7.5, h: 3.4, at: [-3.6, 0]});
  s.add(net);
  const w = {"p>u": 1, "p>v": 2, "q>u": 1, "q>v": -1}, wl = {};
  for (const k in w) { const [a, b] = k.split(">"); wl[k] = net.weight(a, b, String(w[k])); }
  const gu = Tex("1", {color: RED, size: 0.85}).next_to(net.node("u"), UP, 0.15);
  const M = Matrix([["1", "2"], ["1", "-1"]], {at: [3.4, 1.2], color: BLUE});
  s.step("En bleu, les poids de la 2ᵉ couche.", () => { s.play(LaggedStart(...Object.values(wl).map(l => Write(l)))); });
  s.step("La sortie renvoie $\\partial s/\\partial u = 1$.", () => {
    s.play(Flow(net.edge("u", "y"), {reverse: true, color: RED, paint: true}));
    s.play(FadeIn(gu, {shift: [0, -0.3]}));
  });
  s.step("Les poids sortant de p forment la ligne 1 de $W_2^{\\top}$.", () => {
    s.play(Indicate(wl["p>u"]), Indicate(wl["p>v"]));
    s.play(Create(M.brackets));
    s.play(TransformFromCopy(wl["p>u"], M.entry(0, 0)), TransformFromCopy(wl["p>v"], M.entry(0, 1)));
  });
});
```
