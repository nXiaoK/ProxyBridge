import Darwin

func fail(_ message: String) -> Never {
    fputs("\(message)\n", stderr)
    exit(1)
}

guard CommandLine.arguments.count == 3 else {
    fail("usage: atomic_replace_app <staged-path> <final-path>")
}

let stagedPath = CommandLine.arguments[1]
let finalPath = CommandLine.arguments[2]
var finalStat = stat()
let finalExists = finalPath.withCString { lstat($0, &finalStat) == 0 }

let result: Int32 = stagedPath.withCString { stagedPointer in
    finalPath.withCString { finalPointer in
        if finalExists {
            return renameatx_np(
                AT_FDCWD,
                stagedPointer,
                AT_FDCWD,
                finalPointer,
                UInt32(RENAME_SWAP)
            )
        }
        return rename(stagedPointer, finalPointer)
    }
}

if result != 0 {
    let detail = String(cString: strerror(errno))
    fail("atomic app replacement failed: \(detail)")
}
