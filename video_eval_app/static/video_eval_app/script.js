let selectionExists
function selectContents(evt) {
  if (selectionExists) return
  const selection = window.getSelection()
  if (selection.toString() !== "") return
  const el = evt.target
  const range = document.createRange()
  range.selectNodeContents(el)
  selection.removeAllRanges()
  selection.addRange(range)
}
function checkIfSelected() {
  const selection = window.getSelection()
  selectionExists = selection.toString() !== ""
}
function highlightOnClick(el) {
  el.addEventListener('mousedown', checkIfSelected)
  el.addEventListener('click', selectContents)
}
