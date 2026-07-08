import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

struct IconSpec {
    let type: String
    let pixels: Int
}

let specs = [
    IconSpec(type: "icp4", pixels: 16),
    IconSpec(type: "icp5", pixels: 32),
    IconSpec(type: "icp6", pixels: 64),
    IconSpec(type: "ic07", pixels: 128),
    IconSpec(type: "ic08", pixels: 256),
    IconSpec(type: "ic09", pixels: 512),
    IconSpec(type: "ic10", pixels: 1024),
]

let colorSpace = CGColorSpace(name: CGColorSpace.sRGB)!

func rgba(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(red: red / 255, green: green / 255, blue: blue / 255, alpha: alpha)
}

func drawLine(_ context: CGContext, from start: CGPoint, to end: CGPoint, width: CGFloat, stroke: CGColor) {
    context.saveGState()
    context.setLineWidth(width)
    context.setLineCap(.round)
    context.setLineJoin(.round)
    context.setStrokeColor(stroke)
    context.move(to: start)
    context.addLine(to: end)
    context.strokePath()
    context.restoreGState()
}

func drawCircle(_ context: CGContext, center: CGPoint, radius: CGFloat, fill: CGColor, stroke: CGColor? = nil, lineWidth: CGFloat = 0) {
    let rect = CGRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)
    context.saveGState()
    context.setFillColor(fill)
    context.fillEllipse(in: rect)
    if let stroke {
        context.setLineWidth(lineWidth)
        context.setStrokeColor(stroke)
        context.strokeEllipse(in: rect)
    }
    context.restoreGState()
}

func makeIcon(size: Int) -> CGImage {
    guard let context = CGContext(
        data: nil,
        width: size,
        height: size,
        bitsPerComponent: 8,
        bytesPerRow: size * 4,
        space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        fatalError("Could not create CGContext")
    }

    let s = CGFloat(size)
    context.setAllowsAntialiasing(true)
    context.setShouldAntialias(true)
    context.clear(CGRect(x: 0, y: 0, width: s, height: s))

    let body = CGRect(x: s * 0.08, y: s * 0.08, width: s * 0.84, height: s * 0.84)
    let radius = s * 0.19
    let bodyPath = CGPath(roundedRect: body, cornerWidth: radius, cornerHeight: radius, transform: nil)

    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -s * 0.025), blur: s * 0.055, color: rgba(0, 0, 0, 0.32))
    context.addPath(bodyPath)
    context.setFillColor(rgba(7, 13, 25))
    context.fillPath()
    context.restoreGState()

    context.saveGState()
    context.addPath(bodyPath)
    context.clip()
    let gradientColors = [rgba(19, 35, 68), rgba(27, 67, 146), rgba(15, 23, 42)] as CFArray
    let locations: [CGFloat] = [0, 0.55, 1]
    let gradient = CGGradient(colorsSpace: colorSpace, colors: gradientColors, locations: locations)!
    context.drawLinearGradient(
        gradient,
        start: CGPoint(x: body.minX, y: body.maxY),
        end: CGPoint(x: body.maxX, y: body.minY),
        options: []
    )
    context.restoreGState()

    let glow = CGPath(
        roundedRect: body.insetBy(dx: s * 0.055, dy: s * 0.055),
        cornerWidth: radius * 0.75,
        cornerHeight: radius * 0.75,
        transform: nil
    )
    context.addPath(glow)
    context.setFillColor(rgba(91, 120, 255, 0.12))
    context.fillPath()

    let inner = CGPath(
        roundedRect: body.insetBy(dx: s * 0.012, dy: s * 0.012),
        cornerWidth: radius * 0.94,
        cornerHeight: radius * 0.94,
        transform: nil
    )
    context.addPath(inner)
    context.setLineWidth(max(1, s * 0.012))
    context.setStrokeColor(rgba(255, 255, 255, 0.16))
    context.strokePath()

    let bridgeBlue = rgba(168, 195, 255)
    let bridgeBlueSoft = rgba(87, 133, 255, 0.36)
    let green = rgba(106, 216, 118)
    let cyan = rgba(83, 198, 255)
    let line = max(2.2, s * 0.043)

    drawLine(
        context,
        from: CGPoint(x: s * 0.29, y: s * 0.30),
        to: CGPoint(x: s * 0.71, y: s * 0.30),
        width: max(1.5, s * 0.018),
        stroke: rgba(91, 120, 255, 0.20)
    )

    drawLine(context, from: CGPoint(x: s * 0.26, y: s * 0.43), to: CGPoint(x: s * 0.74, y: s * 0.43), width: line, stroke: bridgeBlue)
    drawLine(context, from: CGPoint(x: s * 0.30, y: s * 0.52), to: CGPoint(x: s * 0.70, y: s * 0.52), width: max(1.6, line * 0.45), stroke: bridgeBlueSoft)
    drawLine(context, from: CGPoint(x: s * 0.34, y: s * 0.42), to: CGPoint(x: s * 0.34, y: s * 0.66), width: max(1.9, line * 0.62), stroke: bridgeBlue)
    drawLine(context, from: CGPoint(x: s * 0.66, y: s * 0.42), to: CGPoint(x: s * 0.66, y: s * 0.66), width: max(1.9, line * 0.62), stroke: bridgeBlue)

    context.saveGState()
    context.setLineCap(.round)
    context.setLineJoin(.round)
    context.setLineWidth(max(1.8, line * 0.52))
    context.setStrokeColor(bridgeBlue)
    context.move(to: CGPoint(x: s * 0.34, y: s * 0.62))
    context.addCurve(
        to: CGPoint(x: s * 0.66, y: s * 0.62),
        control1: CGPoint(x: s * 0.42, y: s * 0.77),
        control2: CGPoint(x: s * 0.58, y: s * 0.77)
    )
    context.strokePath()
    context.restoreGState()

    context.saveGState()
    context.setLineCap(.round)
    context.setLineWidth(max(1.8, line * 0.50))
    context.setStrokeColor(rgba(168, 195, 255, 0.78))
    context.move(to: CGPoint(x: s * 0.30, y: s * 0.43))
    context.addCurve(
        to: CGPoint(x: s * 0.70, y: s * 0.43),
        control1: CGPoint(x: s * 0.41, y: s * 0.26),
        control2: CGPoint(x: s * 0.59, y: s * 0.26)
    )
    context.strokePath()
    context.restoreGState()

    drawCircle(context, center: CGPoint(x: s * 0.27, y: s * 0.65), radius: s * 0.036, fill: cyan, stroke: rgba(255, 255, 255, 0.64), lineWidth: max(1, s * 0.006))
    drawCircle(context, center: CGPoint(x: s * 0.73, y: s * 0.65), radius: s * 0.036, fill: green, stroke: rgba(255, 255, 255, 0.64), lineWidth: max(1, s * 0.006))
    drawCircle(context, center: CGPoint(x: s * 0.43, y: s * 0.43), radius: s * 0.018, fill: rgba(255, 255, 255, 0.72))
    drawCircle(context, center: CGPoint(x: s * 0.50, y: s * 0.43), radius: s * 0.018, fill: green)
    drawCircle(context, center: CGPoint(x: s * 0.57, y: s * 0.43), radius: s * 0.018, fill: rgba(255, 255, 255, 0.72))

    guard let image = context.makeImage() else {
        fatalError("Could not create image")
    }
    return image
}

func pngData(_ image: CGImage) -> Data {
    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(data, UTType.png.identifier as CFString, 1, nil) else {
        fatalError("Could not create PNG destination")
    }
    CGImageDestinationAddImage(destination, image, nil)
    if !CGImageDestinationFinalize(destination) {
        fatalError("Could not write PNG")
    }
    return data as Data
}

func appendASCII(_ value: String, to data: inout Data) {
    guard let bytes = value.data(using: .ascii), bytes.count == 4 else {
        fatalError("ICNS chunk identifiers must be four ASCII bytes")
    }
    data.append(bytes)
}

func appendUInt32BE(_ value: Int, to data: inout Data) {
    var number = UInt32(value).bigEndian
    withUnsafeBytes(of: &number) { data.append(contentsOf: $0) }
}

func writeICNS(_ chunks: [(type: String, png: Data)], to url: URL) throws {
    let totalLength = 8 + chunks.reduce(0) { $0 + 8 + $1.png.count }
    var data = Data()
    appendASCII("icns", to: &data)
    appendUInt32BE(totalLength, to: &data)
    for chunk in chunks {
        appendASCII(chunk.type, to: &data)
        appendUInt32BE(8 + chunk.png.count, to: &data)
        data.append(chunk.png)
    }
    try data.write(to: url)
}

let arguments = Array(CommandLine.arguments.dropFirst())
let outputPath = arguments.first ?? "macos/ProxyBridgeMenuBar/AppIcon.icns"
let outputURL = URL(fileURLWithPath: outputPath)
let fileManager = FileManager.default
try fileManager.createDirectory(at: outputURL.deletingLastPathComponent(), withIntermediateDirectories: true)

let chunks = specs.map { spec in
    (type: spec.type, png: pngData(makeIcon(size: spec.pixels)))
}
try writeICNS(chunks, to: outputURL)

let previewPNG = pngData(makeIcon(size: 1024))
for pngPath in arguments.dropFirst() {
    let pngURL = URL(fileURLWithPath: pngPath)
    try fileManager.createDirectory(at: pngURL.deletingLastPathComponent(), withIntermediateDirectories: true)
    try previewPNG.write(to: pngURL)
}

print(outputURL.path)
