// PostCSS config — mirrors appfrontend/postcss.config.cjs so Mantine's
// CSS-in-JS pipeline behaves identically across the two apps.

module.exports = {
    plugins: {
        "postcss-preset-mantine": {},
        "postcss-simple-vars": {
            variables: {
                "mantine-breakpoint-xs": "30em",
                "mantine-breakpoint-sm": "48em",
                "mantine-breakpoint-md": "64em",
                "mantine-breakpoint-lg": "74em",
                "mantine-breakpoint-xl": "90em",
                "mantine-breakpoint-xxl": "140em",
            },
        },
    },
};
