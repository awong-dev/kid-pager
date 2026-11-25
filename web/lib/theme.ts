import { createTheme, responsiveFontSizes } from "@mui/material/styles";

let theme = createTheme({
  palette: {
    mode: "light",
    // High contrast and colour-blind safe: every `main` is at least 4.5:1 on white except
    // secondary (3:1, a graphic and chip colour that carries dark text). Hues are split along the
    // blue/yellow axis and by lightness so they stay apart under red-green colour blindness;
    // error and warning are the closest pair, so never tell those two apart by colour alone.
    primary: { main: "#0b3c6f" },
    secondary: { main: "#e07b00", contrastText: "#14181f" },
    error: { main: "#a3120f" },
    warning: { main: "#a06500" },
    success: { main: "#00695c" },
    info: { main: "#0277bd" },
    text: { primary: "#14181f", secondary: "#3d4653" },
    divider: "rgba(20, 24, 31, 0.24)",
  },
  shape: { borderRadius: 8 },
  components: {
    // Phones: a bare TextField fills its row unless `sx` sets a width; in a table cell it does not.
    MuiTextField: {
      styleOverrides: {
        root: ({ theme }) => ({
          [theme.breakpoints.down("sm")]: {
            width: "100%",
            ".MuiTableCell-root &": { width: "auto" },
          },
        }),
      },
    },
    // Phones: cells never wrap; the TableContainer scrolls sideways. `overflowWrap: "anywhere"`
    // would wrap cells one character per line.
    MuiTableCell: {
      styleOverrides: {
        root: ({ theme }) => ({
          [theme.breakpoints.down("md")]: { whiteSpace: "nowrap" },
        }),
      },
    },
    MuiTypography: {
      styleOverrides: {
        h4: { overflowWrap: "break-word" },
        h5: { overflowWrap: "break-word" },
        h6: { overflowWrap: "break-word" },
      },
    },
  },
});
theme = responsiveFontSizes(theme);

export default theme;
