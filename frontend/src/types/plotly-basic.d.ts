// plotly.js-basic-dist ships no types; it is the plotly.js API (scatter/bar/pie only).
declare module "plotly.js-basic-dist" {
  import * as Plotly from "plotly.js";
  export default Plotly;
}
