import { createTheme, responsiveFontSizes } from "@mui/material/styles";

let theme = createTheme({
  palette: {
    mode: "light",
    primary: { main: "#2f5d8a" },
    secondary: { main: "#8a5d2f" },
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
