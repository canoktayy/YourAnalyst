// osascript -l JavaScript make_icon.js <logo.png> <out.png> [size] [mark|markicon]
// From "your analyst dashboard içi logo.png" (blue X mark + wordmark), cuts out the X mark:
//   mark      the dashboard's header mark (tight square, no margin or shadow)
//   markicon  the macOS app icon (1024 px with the system's margin and soft shadow)
// Without a mode it works on "YOURANALYST LOGO.png" (a dark square with the wordmark inside). The sources have a checkerboard painted into their
// pixels (no alpha), so the dark rounded square is found by colour, cropped and re-clipped
// onto a transparent canvas.
ObjC.import('AppKit');

function run(argv) {
    const src = $.NSImage.alloc.initWithContentsOfFile(argv[0]);
    const rep = $.NSBitmapImageRep.imageRepWithData(src.TIFFRepresentation);
    const w = rep.pixelsWide, h = rep.pixelsHigh;
    const markMode = argv[3] === 'mark' || argv[3] === 'markicon';   // find the X mark next to a wordmark
    const dark = (x, y) => {
        const c = rep.colorAtXY(x, y).colorUsingColorSpace($.NSColorSpace.deviceRGBColorSpace);
        const r = c.redComponent, g = c.greenComponent, b = c.blueComponent;
        // mark: the square's top is a lighter blue, so count anything coloured or dark;
        // the checkerboard and the white wordmark are both neutral and light
        if (markMode) return Math.max(r, g, b) - Math.min(r, g, b) > 0.15 || (r + g + b) / 3 < 0.45;
        return r < 0.25 && g < 0.25 && b < 0.35;
    };
    // Rows/columns with at least a tenth of the darkest row/column's dark pixels
    // (the square may fill the image or only its left part, next to a wordmark).
    const span = (n, m, test) => {
        const counts = [];
        for (let i = 0; i < n; i += 2) {
            let k = 0;
            for (let j = 0; j < m; j += 8) if (test(i, j)) k++;
            counts.push([i, k]);
        }
        const max = Math.max(...counts.map(c => c[1]));
        const keep = counts.filter(c => c[1] >= max / 10).map(c => c[0]);
        return [Math.min(...keep), Math.max(...keep)];
    };
    let [x0, x1] = span(w, h, (i, j) => dark(i, j));
    let [y0, y1] = span(h, w, (i, j) => dark(j, i));   // rows from the top
    if (markMode) x1 = x0 + (y1 - y0);   // a square; the wordmark to its right is tinted blue
    console.log('square', x0, x1, y0, y1);

    const inset = 6;
    // mark: the source square is wider than tall, so x0 + height cut its right side and
    // the X looked off-centre. Find the real right edge on a row below the X (first neutral
    // checkerboard pixel), then crop the largest square around the square's centre.
    if (markMode) {
        const ym = Math.round(y1 - (y1 - y0) * 0.15);   // a row below the X, inside the square
        const tinted = x => {
            const c = rep.colorAtXY(x, ym).colorUsingColorSpace($.NSColorSpace.deviceRGBColorSpace);
            const r = c.redComponent, g = c.greenComponent, b = c.blueComponent;
            return Math.max(r, g, b) - Math.min(r, g, b) > 0.15 || (r + g + b) / 3 < 0.45;
        };
        let xr = x0 + (y1 - y0) / 2;
        while (xr < w - 1 && tinted(xr + 1)) xr++;
        x1 = xr;
        const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, half = Math.min(x1 - x0, y1 - y0) / 2;
        console.log('real square', x0, x1, 'centre', cx, cy, 'half', half);
        x0 = cx - half; x1 = cx + half; y0 = cy - half; y1 = cy + half;
    }
    const mark = argv[3] === 'mark';                                  // tight crop, no margin or shadow
    const S = Number(argv[2] || 1024), side = mark ? S : Math.round(S * 824 / 1024), pad = (S - side) / 2;
    const out = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
        null, S, S, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
    const ctx = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(out);
    $.NSGraphicsContext.saveGraphicsState;
    $.NSGraphicsContext.setCurrentContext(ctx);
    ctx.imageInterpolation = $.NSImageInterpolationHigh;

    const box = $.NSMakeRect(pad, mark ? 0 : pad + S / 100, side, side);
    const r = side * 0.225;
    const path = $.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius(box, r, r);

    if (!mark) {
    $.NSGraphicsContext.currentContext.saveGraphicsState;
    const shadow = $.NSShadow.alloc.init;
    shadow.shadowOffset = $.NSMakeSize(0, -S / 100);
    shadow.shadowBlurRadius = S * 24 / 1024;
    shadow.shadowColor = $.NSColor.colorWithCalibratedWhiteAlpha(0, 0.35);
    shadow.set;
    $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.05, 0.09, 0.16, 1).setFill;
    path.fill;
    $.NSGraphicsContext.currentContext.restoreGraphicsState;
    }

    path.addClip;
    // NSImage coordinates start at the bottom-left; the crop box was measured from the top.
    const from = $.NSMakeRect(x0 + inset, h - y1 + inset, (x1 - x0) - 2 * inset, (y1 - y0) - 2 * inset);
    const img = $.NSImage.alloc.initWithSize($.NSMakeSize(w, h));
    img.addRepresentation(rep);
    img.drawInRectFromRectOperationFraction(box, from, $.NSCompositingOperationSourceOver, 1);

    $.NSGraphicsContext.restoreGraphicsState;
    out.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $()).writeToFileAtomically(argv[1], true);
    return 'wrote ' + argv[1];
}
