public struct GridPosition: Hashable, Sendable {
    public var column: Int
    public var row: Int
    public init(column: Int, row: Int) {
        self.column = column
        self.row = row
    }
}

public enum CellKind: Equatable, Sendable {
    case mushroom(damage: Int)
    case poisonedMushroom(damage: Int)
    case segment(isHead: Bool)
    case player
    case shot
    case spider
    case flea
    case scorpion
}

public struct FrameSnapshot: Sendable {
    public var columns: Int
    public var rows: Int
    public var cells: [GridPosition: CellKind]
    public init(columns: Int = 30, rows: Int = 30, cells: [GridPosition: CellKind] = [:]) {
        self.columns = columns
        self.rows = rows
        self.cells = cells
    }
}
